"""Description backends: an OpenAI-compatible server, or Apple's `fm` CLI."""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path

from .config import Config

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class BackendError(RuntimeError):
    pass


class Backend(ABC):
    """Produces a short natural-language description of an image."""

    @abstractmethod
    def describe(self, image: Path) -> str: ...

    @abstractmethod
    def check(self) -> str:
        """Raise BackendError if unusable; otherwise return a status line."""


def make_backend(cfg: Config) -> Backend:
    if cfg.backend_kind == "openai":
        return OpenAIBackend(cfg)
    if cfg.backend_kind == "fm":
        return FmBackend(cfg)
    raise BackendError(f"unknown backend kind: {cfg.backend_kind!r}")


def downscale(image: Path, max_px: int) -> tuple[Path, tempfile.TemporaryDirectory | None]:
    """Shrink an image with `sips` so the model sees fewer tokens.

    Returns the path to use and the tempdir keeping it alive (None if unchanged).
    """
    if max_px <= 0 or not shutil.which("sips"):
        return image, None
    tmp = tempfile.TemporaryDirectory(prefix="ssrename-")
    out = Path(tmp.name) / image.name
    result = subprocess.run(
        ["sips", "-Z", str(max_px), str(image), "--out", str(out)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not out.exists():
        tmp.cleanup()
        return image, None
    return out, tmp


def clean(text: str) -> str:
    text = _THINK_BLOCK.sub("", text or "")
    return text.strip().strip('"').strip()


class OpenAIBackend(Backend):
    """Any server speaking /v1/chat/completions with image_url content parts.

    Verified against LM Studio; also works with Ollama's OpenAI-compatible
    endpoint and vLLM.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.oa = cfg.openai

    def _post(self, path: str, body: dict, timeout: float) -> dict:
        req = urllib.request.Request(
            f"{self.oa.base_url}{path}",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.oa.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise BackendError(
                f"{self.oa.base_url}{path} returned {e.code}: {e.read().decode()[:400]}"
            ) from e
        except OSError as e:
            raise BackendError(f"cannot reach {self.oa.base_url}: {e}") from e

    def describe(self, image: Path) -> str:
        path, tmp = downscale(image, self.cfg.max_image_px)
        try:
            b64 = base64.b64encode(path.read_bytes()).decode()
        finally:
            if tmp:
                tmp.cleanup()
        mime = "image/jpeg" if image.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
        body = {
            "model": self.oa.model,
            "max_tokens": self.oa.max_tokens,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": self.cfg.instructions},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self.cfg.prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        },
                    ],
                },
            ],
        }
        body.update(self.oa.extra_body)
        data = self._post("/chat/completions", body, self.oa.timeout)
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise BackendError(f"unexpected response: {json.dumps(data)[:400]}") from e
        text = clean(message.get("content", ""))
        if not text:
            raise BackendError(
                "model returned no description "
                "(if it is a reasoning model, raise backend.openai.max_tokens or "
                "keep thinking disabled in extra_body)"
            )
        return text

    def check(self) -> str:
        try:
            with urllib.request.urlopen(
                urllib.request.Request(
                    f"{self.oa.base_url}/models",
                    headers={"Authorization": f"Bearer {self.oa.api_key}"},
                ),
                timeout=10,
            ) as resp:
                data = json.loads(resp.read())
        except OSError as e:
            raise BackendError(f"cannot reach {self.oa.base_url}: {e}") from e
        ids = [m.get("id") for m in data.get("data", [])]
        if self.oa.model not in ids:
            raise BackendError(
                f"model {self.oa.model!r} not served by {self.oa.base_url}; "
                f"available: {', '.join(ids) or '(none)'}"
            )
        return f"openai backend: {self.oa.model} at {self.oa.base_url}"


class FmBackend(Backend):
    """Apple's `fm` CLI, which ships with macOS 27."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.fm = cfg.fm

    def _run(self, args: list[str], timeout: float) -> str:
        try:
            result = subprocess.run(
                [self.fm.binary, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as e:
            raise BackendError(
                f"{self.fm.binary!r} not found. The fm CLI ships with macOS 27; "
                "on macOS 26 use the openai backend instead."
            ) from e
        except subprocess.TimeoutExpired as e:
            raise BackendError(f"fm timed out after {timeout}s") from e
        if result.returncode != 0:
            raise BackendError(f"fm failed ({result.returncode}): {result.stderr[:400]}")
        return result.stdout

    def describe(self, image: Path) -> str:
        path, tmp = downscale(image, self.cfg.max_image_px)
        try:
            args = [
                "respond",
                self.cfg.prompt,
                "--image",
                str(path),
                "--instructions",
                self.cfg.instructions,
            ]
            if self.fm.model and self.fm.model != "device":
                args += ["--model", self.fm.model]
            text = clean(self._run(args, self.fm.timeout))
        finally:
            if tmp:
                tmp.cleanup()
        if not text:
            raise BackendError("fm returned no description")
        return text

    def check(self) -> str:
        if not shutil.which(self.fm.binary):
            raise BackendError(
                f"{self.fm.binary!r} not found on PATH. The fm CLI ships with "
                "macOS 27; on macOS 26 use the openai backend."
            )
        self._run(["--help"], 30)
        return f"fm backend: model={self.fm.model}"
