"""Turning a model description plus a file date into a filename."""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path

#: The capture moment, as it appears in a filename. Tried in order.
_DATETIME_IN_NAME = (
    # "Screenshot 2026-07-31 at 6.59.43 AM.png", and the older "Screen Shot ...".
    # The 12-hour clock and the AM/PM suffix are locale-dependent, so both the
    # one-digit hour and a missing suffix have to parse.
    re.compile(
        r"(?P<Y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})"
        r"\D{1,5}"
        r"(?P<H>\d{1,2})\.(?P<M>\d{2})\.\d{2}"
        r"\s*(?P<ampm>[AaPp][Mm])?"
    ),
    # Our own output, so a second pass over a renamed file keeps its timestamp.
    re.compile(r"(?P<Y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})-(?P<H>\d{2})-(?P<M>\d{2})"),
    # A bare date, from a file named by something else.
    re.compile(r"(?P<Y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})"),
)

_STOPWORDS = {"a", "an", "the", "of", "in", "on", "at", "for", "with", "and", "to"}

MAX_STEM_LENGTH = 60

#: yyyy-mm-dd-HH-mm, the 24-hour clock. Colons are not usable in a filename and
#: a bare "1037" is hard to read, so the separator stays "-" throughout.
TIMESTAMP_FORMAT = "%Y-%m-%d-%H-%M"


def _from_name(name: str) -> datetime | None:
    for pattern in _DATETIME_IN_NAME:
        m = pattern.search(name)
        if not m:
            continue
        groups = m.groupdict()
        hour = int(groups["H"]) if groups.get("H") else 0
        ampm = (groups.get("ampm") or "").lower()
        if ampm:
            # 12 AM is hour 0 and 12 PM is hour 12; every other PM hour adds 12.
            hour = hour % 12 + (12 if ampm == "pm" else 0)
        try:
            return datetime(
                int(groups["Y"]),
                int(groups["m"]),
                int(groups["d"]),
                hour,
                int(groups["M"]) if groups.get("M") else 0,
            )
        except ValueError:
            continue
    return None


def datetime_for(path: Path) -> datetime:
    """Best-effort capture moment: the one in the filename, else file birth time.

    The filename wins because it survives copying, while birth time does not.
    """
    from_name = _from_name(path.name)
    if from_name is not None:
        return from_name
    st = path.stat()
    ts = getattr(st, "st_birthtime", None) or st.st_mtime
    return datetime.fromtimestamp(ts)


def date_for(path: Path) -> date:
    """The capture date alone. Kept for callers that do not want the time."""
    return datetime_for(path).date()


def slugify(description: str, max_words: int = 5) -> str:
    """Turn free-form model output into a kebab-case filename fragment."""
    text = unicodedata.normalize("NFKD", description)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    # Models like to answer with quotes, markdown, or a trailing sentence.
    text = text.split("\n")[0]
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    words = [w for w in text.split() if w]

    # Drop leading filler ("a screenshot of the ...") but keep stopwords that fall
    # in the middle of an otherwise good phrase.
    while words and words[0] in _STOPWORDS | {"screenshot", "screen", "image", "picture"}:
        words.pop(0)
    words = [w for w in words if w not in _STOPWORDS]

    if not words:
        return "screenshot"
    slug = "-".join(words[:max_words])
    if len(slug) > MAX_STEM_LENGTH:
        slug = slug[:MAX_STEM_LENGTH].rsplit("-", 1)[0] or slug[:MAX_STEM_LENGTH]
    return slug.strip("-") or "screenshot"


def target_path(source: Path, description: str, max_words: int = 5) -> Path:
    """Full destination path: yyyy-mm-dd-HH-mm-short-description.ext, collision-free."""
    stamp = datetime_for(source).strftime(TIMESTAMP_FORMAT)
    stem = f"{stamp}-{slugify(description, max_words)}"
    ext = source.suffix.lower()
    candidate = source.with_name(f"{stem}{ext}")
    n = 2
    while candidate.exists() and not _same_file(candidate, source):
        candidate = source.with_name(f"{stem}-{n}{ext}")
        n += 1
    return candidate


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False
