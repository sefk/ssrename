"""Configuration loading for ssrename."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .fsutil import canonical_case

DEFAULT_CONFIG_PATH = Path("~/.config/ssrename/config.toml").expanduser()

DEFAULT_INSTRUCTIONS = (
    "You name screenshot files. Given a screenshot, reply with a short description "
    "of what it shows, 2-5 words, lowercase, no punctuation. Name the app, site, or "
    "document and the specific thing on screen (e.g. 'github pull request diff', "
    "'stripe invoice settings', 'terminal pytest failure'). Do not mention that it "
    "is a screenshot. Do not explain."
)

DEFAULT_PROMPT = "Describe this screenshot in 2-5 words for use as a filename."

DEFAULT_CONFIG_TEXT = f'''\
# ssrename configuration.
# Docs: see README.md in the ssrename repo.

[general]
# Directory to watch. Set macOS to save screenshots here with:
#   ssrename set-screenshot-dir
#
# ~/Pictures is deliberate: ~/Desktop, ~/Documents and ~/Downloads are gated by
# macOS privacy controls, and a LaunchAgent cannot show a permission prompt, so
# watching one of those means granting Full Disk Access by hand and re-granting
# it whenever the interpreter's path changes. ~/Pictures needs none of that.
watch_dir = "~/Pictures/Screenshots"

# Only files whose names match this regex are considered. The default matches
# the names macOS gives new screenshots, so already-renamed files are left alone.
filename_pattern = "^(Screenshot|Screen Shot|Screen Recording) "

# Seconds to wait after a file appears before renaming it. macOS writes the file
# only after the corner thumbnail expires, but this leaves extra room so the
# thumbnail workflow (drag, markup, delete) is never disturbed.
debounce_seconds = 8.0

# Safety net: rescan the directory this often (seconds) in case an FSEvents
# notification is missed. 0 disables.
poll_seconds = 60.0

# Extensions to process. Screen recordings are matched by filename_pattern above
# but skipped here unless you add "mov".
extensions = ["png", "jpg", "jpeg"]

# Maximum words kept from the model's description.
max_words = 5

# Longest edge, in pixels, of the copy sent to the model (via `sips`). Retina
# screenshots are huge; shrinking cuts latency and token cost a lot. 0 disables.
max_image_px = 1600

[backend]
# "openai" = any OpenAI-compatible server (LM Studio, Ollama, vLLM, ...).
# "fm"     = Apple's `fm` CLI (ships with macOS 27).
kind = "openai"

[backend.openai]
base_url = "http://localhost:1234/v1"
model = "qwen/qwen3.6-27b"
api_key = "lm-studio"
timeout = 180
max_tokens = 128
# Passed through verbatim in the request body. Reasoning models otherwise spend
# the whole token budget thinking and return an empty answer; LM Studio honours
# reasoning_effort = "none". Some servers want
# {{ chat_template_kwargs = {{ enable_thinking = false }} }} instead.
extra_body = {{ reasoning_effort = "none" }}

[backend.fm]
# "device" for the on-device model, "pcc" for Private Cloud Compute.
model = "device"

[prompt]
instructions = """{DEFAULT_INSTRUCTIONS}"""
text = "{DEFAULT_PROMPT}"
'''


@dataclass
class OpenAIBackendConfig:
    base_url: str = "http://localhost:1234/v1"
    model: str = "qwen/qwen3.6-27b"
    api_key: str = "lm-studio"
    timeout: float = 180.0
    max_tokens: int = 128
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass
class FmBackendConfig:
    model: str = "device"
    binary: str = "fm"
    timeout: float = 180.0


@dataclass
class Config:
    watch_dir: Path = Path("~/Pictures/Screenshots").expanduser()
    filename_pattern: str = "^(Screenshot|Screen Shot|Screen Recording) "
    debounce_seconds: float = 8.0
    poll_seconds: float = 60.0
    extensions: tuple[str, ...] = ("png", "jpg", "jpeg")
    max_words: int = 5
    max_image_px: int = 1600
    backend_kind: str = "openai"
    openai: OpenAIBackendConfig = field(default_factory=OpenAIBackendConfig)
    fm: FmBackendConfig = field(default_factory=FmBackendConfig)
    instructions: str = DEFAULT_INSTRUCTIONS
    prompt: str = DEFAULT_PROMPT
    source: Path | None = None


def load_config(path: Path | None = None) -> Config:
    """Load config from `path`, falling back to defaults for anything missing."""
    path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    cfg = Config(source=path)
    if not path.exists():
        return cfg

    with path.open("rb") as fh:
        data = tomllib.load(fh)

    general = data.get("general", {})
    if "watch_dir" in general:
        cfg.watch_dir = canonical_case(Path(general["watch_dir"]))
    cfg.filename_pattern = general.get("filename_pattern", cfg.filename_pattern)
    cfg.debounce_seconds = float(general.get("debounce_seconds", cfg.debounce_seconds))
    cfg.poll_seconds = float(general.get("poll_seconds", cfg.poll_seconds))
    cfg.extensions = tuple(
        e.lower().lstrip(".") for e in general.get("extensions", cfg.extensions)
    )
    cfg.max_words = int(general.get("max_words", cfg.max_words))
    cfg.max_image_px = int(general.get("max_image_px", cfg.max_image_px))

    backend = data.get("backend", {})
    cfg.backend_kind = backend.get("kind", cfg.backend_kind)
    oa = backend.get("openai", {})
    cfg.openai = OpenAIBackendConfig(
        base_url=oa.get("base_url", cfg.openai.base_url).rstrip("/"),
        model=oa.get("model", cfg.openai.model),
        api_key=oa.get("api_key", cfg.openai.api_key),
        timeout=float(oa.get("timeout", cfg.openai.timeout)),
        max_tokens=int(oa.get("max_tokens", cfg.openai.max_tokens)),
        extra_body=dict(oa.get("extra_body", {})),
    )
    fm = backend.get("fm", {})
    cfg.fm = FmBackendConfig(
        model=fm.get("model", cfg.fm.model),
        binary=fm.get("binary", cfg.fm.binary),
        timeout=float(fm.get("timeout", cfg.fm.timeout)),
    )

    prompt = data.get("prompt", {})
    cfg.instructions = prompt.get("instructions", cfg.instructions)
    cfg.prompt = prompt.get("text", cfg.prompt)
    return cfg


def write_default_config(path: Path | None = None, force: bool = False) -> Path:
    """Write the commented default config, returning where it landed."""
    path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    if path.exists() and not force:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG_TEXT)
    return path
