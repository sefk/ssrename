import os
import time
from pathlib import Path

import pytest

from ssrename.config import Config
from ssrename.fsutil import canonical_case
from ssrename.renamer import Renamer
from ssrename.watcher import BACKOFF_MAX, BACKOFF_START, Watcher

from .test_renamer import FakeBackend


def _watcher(tmp_path, backend=None, **kw):
    cfg = Config(watch_dir=tmp_path, **{"debounce_seconds": 0, **kw})
    return Watcher(Renamer(cfg, backend or FakeBackend()))


def _screenshots(tmp_path, n):
    paths = []
    for i in range(n):
        p = tmp_path / f"Screenshot 2026-07-31 at 6.59.4{i} AM.png"
        p.write_bytes(b"not really a png")
        paths.append(p)
    return paths


def test_enqueue_ignores_non_candidates(tmp_path):
    w = _watcher(tmp_path)
    w.enqueue(tmp_path / "notes.txt")
    w.enqueue(tmp_path / "2026-01-01-already-named.png")
    assert w._pending == {}


def test_enqueue_does_not_push_the_deadline_back(tmp_path):
    w = _watcher(tmp_path, debounce_seconds=5)
    p = tmp_path / "Screenshot 2026-07-31 at 6.59.43 AM.png"
    w.enqueue(p)
    first = w._pending[p]
    time.sleep(0.01)
    w.enqueue(p)
    assert w._pending[p] == first


def test_due_returns_and_clears(tmp_path):
    w = _watcher(tmp_path)
    p = tmp_path / "Screenshot 2026-07-31 at 6.59.43 AM.png"
    w.enqueue(p)
    assert w._due() == [p]
    assert w._due() == []


def test_scan_picks_up_new_files_only(tmp_path):
    w = _watcher(tmp_path)
    old = tmp_path / "Screenshot 2026-01-01 at 1.00.00 AM.png"
    old.write_bytes(b"x")
    os.utime(old, (0, w._started_at - 60))
    new = tmp_path / "Screenshot 2026-07-31 at 6.59.43 AM.png"
    new.write_bytes(b"x")
    w.scan()
    assert list(w._pending) == [new]


def test_canonical_case_fixes_directory_capitalisation(tmp_path):
    (tmp_path / "Screenshots").mkdir()
    fixed = canonical_case(tmp_path / "screenshots")
    assert fixed.name == "Screenshots"


def test_canonical_case_leaves_missing_paths_alone(tmp_path):
    p = tmp_path / "nope" / "deeper"
    assert canonical_case(p) == p


def test_dead_backend_is_probed_once_and_the_queue_is_kept(tmp_path):
    """The bug this guards: with the backend down, every queued screenshot was
    retried every poll, so one dead server produced an unbounded error log."""
    backend = FakeBackend(down=True)
    w = _watcher(tmp_path, backend=backend)
    a, b = _screenshots(tmp_path, 2)
    w.enqueue(a)
    w.enqueue(b)

    w.drain()

    assert len(backend.calls) == 1  # one probe, not one per file
    assert set(w._pending) == {a, b}  # nothing dropped
    assert w._backoff == BACKOFF_START


def test_backoff_holds_off_further_attempts(tmp_path):
    backend = FakeBackend(down=True)
    w = _watcher(tmp_path, backend=backend)
    w.enqueue(_screenshots(tmp_path, 1)[0])

    w.drain()
    w.drain()  # still inside the backoff window

    assert len(backend.calls) == 1


def test_backoff_doubles_up_to_the_ceiling(tmp_path):
    w = _watcher(tmp_path, backend=FakeBackend(down=True))
    w.enqueue(_screenshots(tmp_path, 1)[0])

    seen = []
    for _ in range(12):
        w._retry_at = 0.0  # pretend the wait elapsed
        w.drain()
        seen.append(w._backoff)

    assert seen[0] == BACKOFF_START
    assert seen[1] == BACKOFF_START * 2
    assert seen[-1] == BACKOFF_MAX


def test_recovery_clears_the_backoff_and_renames(tmp_path):
    backend = FakeBackend(down=True)
    w = _watcher(tmp_path, backend=backend)
    src = _screenshots(tmp_path, 1)[0]
    w.enqueue(src)
    w.drain()
    assert w._backoff

    backend.down = False
    w._retry_at = 0.0
    w.drain()

    assert w._backoff == 0.0
    assert w._pending == {}
    assert not src.exists()  # renamed


def test_a_single_bad_file_does_not_trip_the_backoff(tmp_path):
    """A per-file failure says nothing about the backend; keep going."""
    backend = FakeBackend(fail=True)
    w = _watcher(tmp_path, backend=backend)
    a, b = _screenshots(tmp_path, 2)
    w.enqueue(a)
    w.enqueue(b)

    w.drain()

    assert len(backend.calls) == 2
    assert w._backoff == 0.0
    assert w._pending == {}
