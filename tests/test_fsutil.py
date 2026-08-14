import errno
import os

import pytest

from ssrename.fsutil import rename_no_clobber


def test_moves_the_file(tmp_path):
    src, dest = tmp_path / "a.png", tmp_path / "b.png"
    src.write_bytes(b"content")
    rename_no_clobber(src, dest)
    assert dest.read_bytes() == b"content"
    assert not src.exists()


def test_refuses_to_overwrite(tmp_path):
    """os.rename would destroy the destination silently. This must not."""
    src, dest = tmp_path / "a.png", tmp_path / "b.png"
    src.write_bytes(b"new")
    dest.write_bytes(b"a screenshot worth keeping")
    with pytest.raises(FileExistsError):
        rename_no_clobber(src, dest)
    assert dest.read_bytes() == b"a screenshot worth keeping"
    assert src.read_bytes() == b"new"  # nothing lost on either side


def test_leaves_no_hard_link_behind(tmp_path):
    src, dest = tmp_path / "a.png", tmp_path / "b.png"
    src.write_bytes(b"content")
    rename_no_clobber(src, dest)
    assert dest.stat().st_nlink == 1


def test_falls_back_when_hard_links_are_unsupported(tmp_path, monkeypatch):
    def no_links(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "not supported")

    monkeypatch.setattr(os, "link", no_links)
    src, dest = tmp_path / "a.png", tmp_path / "b.png"
    src.write_bytes(b"content")
    rename_no_clobber(src, dest)
    assert dest.read_bytes() == b"content"
    assert not src.exists()


def test_fallback_still_refuses_to_overwrite(tmp_path, monkeypatch):
    def no_links(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "not supported")

    monkeypatch.setattr(os, "link", no_links)
    src, dest = tmp_path / "a.png", tmp_path / "b.png"
    src.write_bytes(b"new")
    dest.write_bytes(b"keep me")
    with pytest.raises(FileExistsError):
        rename_no_clobber(src, dest)
    assert dest.read_bytes() == b"keep me"


def test_unexpected_oserror_is_not_swallowed(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError(errno.EIO, "disk on fire")

    monkeypatch.setattr(os, "link", broken)
    src, dest = tmp_path / "a.png", tmp_path / "b.png"
    src.write_bytes(b"content")
    with pytest.raises(OSError) as excinfo:
        rename_no_clobber(src, dest)
    assert excinfo.value.errno == errno.EIO
