"""Filesystem helpers."""

from __future__ import annotations

import errno
import os
from pathlib import Path


def rename_no_clobber(source: Path, dest: Path) -> None:
    """Move `source` to `dest`, refusing to overwrite an existing `dest`.

    `os.rename` silently destroys the destination, which is the wrong outcome
    for a screenshot: two ssrename processes (the LaunchAgent and a manual
    `once` or `backfill`) can pick the same free name at the same moment, and
    the loser's file would vanish with no error. Two captures in one minute
    showing the same thing is exactly the input that produces that clash.

    Raises FileExistsError if `dest` was taken, so the caller can pick again.
    """
    source, dest = Path(source), Path(dest)
    try:
        # Atomic: fails rather than replacing, unlike rename.
        os.link(source, dest)
    except FileExistsError:
        raise
    except OSError as e:
        # Some filesystems (FAT, a few network mounts) have no hard links. Claim
        # the name with an exclusive create instead, then rename onto the
        # placeholder we know we own.
        if e.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK):
            raise
        fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
        os.rename(source, dest)
        return
    os.unlink(source)


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
