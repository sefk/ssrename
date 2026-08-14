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
    assert result.dest.name == "2026-07-31-06-59-github-pull-request.png"
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
    assert result.dest.name == "2026-07-31-06-59-github-pull-request.png"
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
    (tmp_path / "2026-07-31-06-59-github-pull-request.png").write_bytes(b"old")
    src = _screenshot(tmp_path)
    result = renamer.process(src)
    assert result.dest.name == "2026-07-31-06-59-github-pull-request-2.png"


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


def test_losing_a_race_does_not_destroy_the_other_file(renamer, tmp_path, monkeypatch):
    """Another process claims our chosen name in the gap before we take it."""
    import ssrename.renamer as renamer_mod

    real = renamer_mod.rename_no_clobber
    taken = tmp_path / "2026-07-31-06-59-github-pull-request.png"

    def steal_then_rename(source, dest):
        # Simulate the other process winning, but only for the first choice.
        if dest == taken and not taken.exists():
            taken.write_bytes(b"the other process's screenshot")
        return real(source, dest)

    monkeypatch.setattr(renamer_mod, "rename_no_clobber", steal_then_rename)

    src = _screenshot(tmp_path)
    result = renamer.process(src)

    assert result.renamed
    # We stepped aside instead of overwriting.
    assert result.dest.name == "2026-07-31-06-59-github-pull-request-2.png"
    assert taken.read_bytes() == b"the other process's screenshot"
    assert result.dest.read_bytes() == b"not really a png"


def test_gives_up_after_repeated_losses(renamer, tmp_path, monkeypatch):
    import ssrename.renamer as renamer_mod

    def always_taken(source, dest):
        raise FileExistsError(f"{dest} taken")

    monkeypatch.setattr(renamer_mod, "rename_no_clobber", always_taken)

    result = renamer.process(_screenshot(tmp_path))
    assert not result.renamed
    assert "could not claim a free name" in result.error
