from pathlib import Path

import pytest

from ssrename.backends import Backend, BackendError, clean
from ssrename.config import Config
from ssrename.renamer import Renamer


class FakeBackend(Backend):
    def __init__(self, description="github pull request", fail=False):
        self.description = description
        self.fail = fail
        self.calls: list[Path] = []

    def describe(self, image: Path) -> str:
        self.calls.append(image)
        if self.fail:
            raise BackendError("no model")
        return self.description

    def check(self) -> str:
        return "fake"


@pytest.fixture
def renamer(tmp_path):
    cfg = Config(watch_dir=tmp_path, debounce_seconds=0)
    r = Renamer(cfg, FakeBackend())
    return r


def _screenshot(tmp_path, name="Screenshot 2026-07-31 at 6.59.43 AM.png"):
    p = tmp_path / name
    p.write_bytes(b"not really a png")
    return p


def test_renames_a_screenshot(renamer, tmp_path):
    src = _screenshot(tmp_path)
    result = renamer.process(src)
    assert result.renamed
    assert result.dest.name == "2026-07-31-github-pull-request.png"
    assert result.dest.exists() and not src.exists()


def test_ignores_already_renamed_files(renamer, tmp_path):
    src = _screenshot(tmp_path, "2026-07-31-github-pull-request.png")
    result = renamer.process(src)
    assert result.skipped and not result.renamed
    assert src.exists()


def test_ignores_other_files(renamer, tmp_path):
    for name in ["notes.txt", "cat.png", "Screenshot 2026-07-31 at 1.00.00 AM.txt"]:
        assert not renamer.is_candidate(tmp_path / name)


def test_accepts_narrow_nbsp_names(renamer, tmp_path):
    # macOS puts U+202F before AM/PM.
    assert renamer.is_candidate(tmp_path / "Screenshot 2026-07-31 at 6.59.43 AM.png")


def test_accepts_legacy_screen_shot_names(renamer, tmp_path):
    assert renamer.is_candidate(tmp_path / "Screen Shot 2019-01-02 at 3.04.05 PM.png")


def test_dry_run_leaves_the_file_alone(tmp_path):
    cfg = Config(watch_dir=tmp_path, debounce_seconds=0)
    r = Renamer(cfg, FakeBackend(), dry_run=True)
    src = _screenshot(tmp_path)
    result = r.process(src)
    assert result.dest.name == "2026-07-31-github-pull-request.png"
    assert src.exists()
    assert not result.dest.exists()


def test_backend_failure_leaves_the_file_alone(tmp_path):
    cfg = Config(watch_dir=tmp_path, debounce_seconds=0)
    r = Renamer(cfg, FakeBackend(fail=True))
    src = _screenshot(tmp_path)
    result = r.process(src)
    assert result.error and src.exists()


def test_missing_file_is_skipped(renamer, tmp_path):
    result = renamer.process(tmp_path / "Screenshot 2026-07-31 at 6.59.43 AM.png")
    assert result.skipped == "gone"


def test_collision_gets_a_suffix(renamer, tmp_path):
    (tmp_path / "2026-07-31-github-pull-request.png").write_bytes(b"old")
    src = _screenshot(tmp_path)
    result = renamer.process(src)
    assert result.dest.name == "2026-07-31-github-pull-request-2.png"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("<think>hmm</think>github diff", "github diff"),
        ('  "quoted answer"  ', "quoted answer"),
        ("", ""),
    ],
)
def test_clean(raw, expected):
    assert clean(raw) == expected
