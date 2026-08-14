"""Deciding what to rename, and doing it safely."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .backends import Backend, BackendError
from .config import Config
from .fsutil import rename_no_clobber
from .naming import target_path

log = logging.getLogger("ssrename")

#: How many times to pick a fresh name when another process claims ours first.
#: Each loss means a genuine race, so more than a couple means something is very
#: wrong rather than merely unlucky.
CLAIM_ATTEMPTS = 5


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

        # Another process can take the name between choosing it and claiming it,
        # so choosing is part of the retry, not done once up front.
        for _ in range(CLAIM_ATTEMPTS):
            dest = target_path(path, description, self.cfg.max_words)
            if dest == path:
                return Result(path, skipped="already named that")
            if self.dry_run:
                log.info("%s -> %s  (%s)", path.name, dest.name, description)
                return Result(path, dest=dest, skipped="dry run")
            # Re-check just before moving: the user may have dragged the file out
            # of the thumbnail chip or deleted it while the model was thinking.
            if not path.exists():
                return Result(path, skipped="gone")
            try:
                rename_no_clobber(path, dest)
            except FileExistsError:
                continue  # lost the race; pick the next free name
            except OSError as e:
                return Result(path, error=f"rename failed: {e}")
            log.info("%s -> %s  (%s)", path.name, dest.name, description)
            return Result(path, dest=dest)
        return Result(
            path, error=f"could not claim a free name after {CLAIM_ATTEMPTS} tries"
        )
