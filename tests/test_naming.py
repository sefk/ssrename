from datetime import date
from pathlib import Path

import pytest

from ssrename.naming import date_for, slugify, target_path


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("GitHub pull request diff", "github-pull-request-diff"),
        ("A screenshot of the Stripe invoice settings", "stripe-invoice-settings"),
        ('"Terminal pytest failure"', "terminal-pytest-failure"),
        ("Café menu — prices", "cafe-menu-prices"),
        ("one two three four five six seven", "one-two-three-four-five"),
        ("", "screenshot"),
        ("the of in on", "screenshot"),
        # Only the first line: models like to append an explanation.
        ("Slack thread\nThis screenshot shows...", "slack-thread"),
    ],
)
def test_slugify(raw, expected):
    assert slugify(raw) == expected


def test_slugify_respects_max_words():
    assert slugify("alpha beta gamma delta", max_words=2) == "alpha-beta"


def test_slugify_caps_length():
    assert len(slugify(" ".join(["abcdefghij"] * 10), max_words=99)) <= 60


def test_date_from_filename(tmp_path):
    p = tmp_path / "Screenshot 2026-07-31 at 6.59.43 AM.png"
    p.write_bytes(b"x")
    assert date_for(p) == date(2026, 7, 31)


def test_date_falls_back_to_file_time(tmp_path):
    p = tmp_path / "whatever.png"
    p.write_bytes(b"x")
    assert isinstance(date_for(p), date)


def test_target_path(tmp_path):
    src = tmp_path / "Screenshot 2026-07-31 at 6.59.43 AM.png"
    src.write_bytes(b"x")
    assert target_path(src, "GitHub pull request").name == "2026-07-31-github-pull-request.png"


def test_target_path_avoids_collisions(tmp_path):
    src = tmp_path / "Screenshot 2026-07-31 at 6.59.43 AM.png"
    src.write_bytes(b"x")
    (tmp_path / "2026-07-31-github-pr.png").write_bytes(b"y")
    (tmp_path / "2026-07-31-github-pr-2.png").write_bytes(b"y")
    assert target_path(src, "github pr").name == "2026-07-31-github-pr-3.png"


def test_target_path_normalises_extension_case(tmp_path):
    src = tmp_path / "Screenshot 2026-07-31 at 6.59.43 AM.PNG"
    src.write_bytes(b"x")
    assert target_path(src, "one two").suffix == ".png"


def test_target_path_is_in_same_directory(tmp_path):
    src = tmp_path / "Screenshot 2026-07-31 at 1.00.00 AM.png"
    src.write_bytes(b"x")
    assert target_path(src, "hello world").parent == Path(tmp_path)
