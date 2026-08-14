import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ssrename import system


def test_protected_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for name in ("Desktop", "Documents", "Downloads", "Pictures"):
        (tmp_path / name).mkdir()
    assert system.is_protected(tmp_path / "Desktop" / "Screenshots")
    assert system.is_protected(tmp_path / "Documents")
    assert not system.is_protected(tmp_path / "Pictures" / "Screenshots")


def test_paths_outside_home_are_not_protected(tmp_path):
    assert not system.is_protected(Path("/tmp"))


def test_tcc_binary_is_the_real_interpreter():
    binary = system.tcc_binary()
    assert binary.is_absolute()
    assert not binary.is_symlink()  # resolved, so it matches what TCC lists
    assert binary == Path(sys.executable).resolve()


def test_check_read_access_ok(tmp_path):
    (tmp_path / "a.png").write_bytes(b"x")
    ok, detail = system.check_read_access(tmp_path)
    assert ok and "a.png" in detail


def test_check_read_access_empty_dir(tmp_path):
    ok, detail = system.check_read_access(tmp_path)
    assert ok and "no file to open" in detail


def test_check_read_access_missing_dir(tmp_path):
    ok, detail = system.check_read_access(tmp_path / "nope")
    assert not ok and "cannot list" in detail


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_check_read_access_unlistable_dir(tmp_path):
    d = tmp_path / "locked"
    d.mkdir()
    (d / "a.png").write_bytes(b"x")
    d.chmod(0)
    try:
        ok, detail = system.check_read_access(d)
        assert not ok and "cannot list" in detail
    finally:
        d.chmod(stat.S_IRWXU)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_check_read_access_unreadable_file(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    f.chmod(0)
    try:
        ok, detail = system.check_read_access(tmp_path)
        assert not ok and "cannot read" in detail
    finally:
        f.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_agent_log_tail_selects_errors(tmp_path, monkeypatch):
    log = tmp_path / "ssrename.log"
    log.write_text(
        "2026-01-01 INFO watching\n"
        "2026-01-01 ERROR shot.png: cannot reach server\n"
        "2026-01-01 INFO renamed\n"
    )
    monkeypatch.setattr(system, "LOG_PATH", log)
    assert system.agent_log_tail() == ["2026-01-01 ERROR shot.png: cannot reach server"]


def test_agent_log_tail_without_a_log(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "LOG_PATH", tmp_path / "missing.log")
    assert system.agent_log_tail() == []


def test_set_screenshot_location_writes_every_key(tmp_path, monkeypatch):
    """macOS 27 reads location-screenshot; older macOS reads location."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(system.subprocess, "run", fake_run)
    target = tmp_path / "Screenshots"
    system.set_screenshot_location(target)

    assert target.is_dir()
    written = {
        argv[3]: argv[4]
        for argv in calls
        if argv[:3] == ["defaults", "write", "com.apple.screencapture"]
    }
    assert written == {"location-screenshot": str(target), "location": str(target)}


def test_screenshot_location_prefers_the_modern_key(monkeypatch):
    values = {"location-screenshot": "/new/place", "location": "/stale/place"}

    def fake_run(argv, **kwargs):
        key = argv[3]
        if key in values:
            return subprocess.CompletedProcess(argv, 0, values[key] + "\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "not found")

    monkeypatch.setattr(system.subprocess, "run", fake_run)
    assert system.screenshot_location() == "/new/place"


def test_screenshot_location_falls_back_to_the_legacy_key(monkeypatch):
    def fake_run(argv, **kwargs):
        if argv[3] == "location":
            return subprocess.CompletedProcess(argv, 0, "/old/place\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "not found")

    monkeypatch.setattr(system.subprocess, "run", fake_run)
    assert system.screenshot_location() == "/old/place"
