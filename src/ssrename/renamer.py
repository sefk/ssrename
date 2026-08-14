"""Deciding what to rename, and doing it safely."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .backends import Backend, BackendError
from .config import Config
from .naming import target_path

log = logging.getLogger("ssrename")


@dataclass
class Result:
    source: Path
    dest: Path | None = None
    skipped: str | None = None
    error: str | None = None

    @property
    def renamed(self) -> bool:
        return self.dest is not None and self.error is None and self.skipped is None


class Renamer:
    def __init__(self, cfg: Config, backend: Backend, dry_run: bool = False):
        self.cfg = cfg
        self.backend = backend
        self.dry_run = dry_run
        self.pattern = re.compile(cfg.filename_pattern)

    def is_candidate(self, path: Path) -> bool:
        """Only untouched macOS screenshots, never our own output."""
        if path.name.startswith("."):
            return False
        if path.suffix.lower().lstrip(".") not in self.cfg.extensions:
            return False
        # macOS uses a narrow no-break space before AM/PM; normalise for matching.
        return bool(self.pattern.match(path.name.replace(" ", " ")))

    def wait_until_stable(self, path: Path, timeout: float = 60.0) -> bool:
        """Wait for the file to stop growing before reading it."""
        deadline = time.monotonic() + timeout
        last = None
        stable = 0
        while time.monotonic() < deadline:
            try:
                sig = path.stat().st_size, path.stat().st_mtime
            except FileNotFoundError:
                return False
            if sig == last and sig[0] > 0:
                stable += 1
                if stable >= 2:
                    return True
            else:
                stable = 0
            last = sig
            time.sleep(0.5)
        return False

    def process(self, path: Path) -> Result:
        path = Path(path)
        if not path.exists():
            return Result(path, skipped="gone")
        if not self.is_candidate(path):
            return Result(path, skipped="not a fresh screenshot")
        if not self.wait_until_stable(path):
            return Result(path, skipped="never stopped changing (or was moved)")

        try:
            description = self.backend.describe(path)
        except BackendError as e:
            return Result(path, error=str(e))

        dest = target_path(path, description, self.cfg.max_words)
        if dest == path:
            return Result(path, skipped="already named that")
        log.info("%s -> %s  (%s)", path.name, dest.name, description)
        if self.dry_run:
            return Result(path, dest=dest, skipped="dry run")
        try:
            # Re-check just before moving: the user may have dragged the file out
            # of the thumbnail chip or deleted it while the model was thinking.
            if not path.exists():
                return Result(path, skipped="gone")
            path.rename(dest)
        except OSError as e:
            return Result(path, error=f"rename failed: {e}")
        return Result(path, dest=dest)
