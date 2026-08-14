"""Turning a model description plus a file date into a filename."""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path

# "Screenshot 2026-07-31 at 6.59.43 AM.png" and the older "Screen Shot 2026-07-31 at ..."
_DATE_IN_NAME = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

_STOPWORDS = {"a", "an", "the", "of", "in", "on", "at", "for", "with", "and", "to"}

MAX_STEM_LENGTH = 60


def date_for(path: Path) -> date:
    """Best-effort capture date: the date in the filename, else file birth time."""
    m = _DATE_IN_NAME.search(path.name)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            pass
    st = path.stat()
    ts = getattr(st, "st_birthtime", None) or st.st_mtime
    return datetime.fromtimestamp(ts).date()


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
    """Full destination path: yyyy-mm-dd-short-description.ext, collision-free."""
    stem = f"{date_for(source).isoformat()}-{slugify(description, max_words)}"
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
