import os
import time
from pathlib import Path

import pytest

from ssrename.config import Config
from ssrename.fsutil import canonical_case
from ssrename.renamer import Renamer
from ssrename.watcher import Watcher

from .test_renamer import FakeBackend


def _watcher(tmp_path, **kw):
    cfg = Config(watch_dir=tmp_path, **{"debounce_seconds": 0, **kw})
    return Watcher(Renamer(cfg, FakeBackend()))


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
