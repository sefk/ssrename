"""Filesystem helpers."""

from __future__ import annotations

import os
from pathlib import Path


def canonical_case(path: Path) -> Path:
    """Return `path` with each component's real on-disk capitalisation.

    APFS is case-insensitive, so `~/Desktop/screenshots` opens a directory really
    named `Screenshots`. watchdog compares FSEvents paths to the watched path as
    strings, and silently drops everything when the case differs — so fix it up
    front rather than debugging it later.
    """
    path = Path(path).expanduser()
    parts = path.parts
    if not parts:
        return path
    current = Path(parts[0])
    for part in parts[1:]:
        try:
            match = next(
                (e.name for e in os.scandir(current) if e.name.lower() == part.lower()),
                None,
            )
        except OSError:
            match = None
        current = current / (match or part)
    return current
