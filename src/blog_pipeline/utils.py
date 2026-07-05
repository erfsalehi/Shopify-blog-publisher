"""Small shared helpers: slugify, HTML text extraction, keyword tokenizing."""

from __future__ import annotations

import re
from html.parser import HTMLParser


def slugify(text: str, max_len: int = 80) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return text[:max_len].rstrip("-") or "post"


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    @property
    def text(self) -> str:
        return " ".join(" ".join(self._chunks).split())


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html or "")
    return parser.text


def word_count(html_or_text: str) -> int:
    text = html_to_text(html_or_text) if "<" in html_or_text else html_or_text
    return len(text.split())


_WORD_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def keyword_overlap(a: str, b: str) -> float:
    """Jaccard overlap of word tokens — cheap exact-ish dedup signal."""
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
