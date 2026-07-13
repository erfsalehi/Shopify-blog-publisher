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
        self._skip = 0  # depth inside <script>/<style> — never visible prose

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._chunks.append(data)

    @property
    def text(self) -> str:
        return " ".join(" ".join(self._chunks).split())


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html or "")
    return parser.text


class _MarkdownExtractor(HTMLParser):
    """Converts the tag set our draft/SEO/image stages actually emit —
    h2/h3, p, ul/ol/li, strong/em, a, img, figure — into Markdown. Not a
    general HTML->Markdown converter; anything outside that vocabulary
    passes through as plain text."""

    def __init__(self) -> None:
        super().__init__()
        self._out: list[str] = []
        self._list_stack: list[dict] = []
        self._in_link = False
        self._link_href = ""
        self._link_text: list[str] = []
        self._skip = 0  # inside <script>/<style> — JSON-LD etc., not prose
        self._in_blockquote = False  # single-paragraph pull-quotes only

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
            return
        attrs_d = dict(attrs)
        if tag == "h2":
            self._out.append("\n\n## ")
        elif tag == "h3":
            self._out.append("\n\n### ")
        elif tag == "blockquote":
            self._out.append("\n\n> ")
            self._in_blockquote = True
        elif tag == "p":
            if not self._in_blockquote:
                self._out.append("\n\n")
        elif tag == "ul":
            self._list_stack.append({"type": "ul", "n": 0})
        elif tag == "ol":
            self._list_stack.append({"type": "ol", "n": 0})
        elif tag == "li":
            if self._list_stack and self._list_stack[-1]["type"] == "ol":
                self._list_stack[-1]["n"] += 1
                self._out.append(f"\n{self._list_stack[-1]['n']}. ")
            else:
                self._out.append("\n- ")
        elif tag in ("strong", "b"):
            self._out.append("**")
        elif tag in ("em", "i"):
            self._out.append("*")
        elif tag == "a":
            self._in_link = True
            self._link_href = attrs_d.get("href") or ""
            self._link_text = []
        elif tag == "img":
            self._out.append(f"\n\n![{attrs_d.get('alt') or ''}]({attrs_d.get('src') or ''})\n\n")
        elif tag == "br":
            self._out.append("  \n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            if self._skip:
                self._skip -= 1
            return
        if tag in ("h2", "h3"):
            self._out.append("\n")
        elif tag == "blockquote":
            self._out.append("\n\n")
            self._in_blockquote = False
        elif tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            self._out.append("\n")
        elif tag in ("strong", "b"):
            self._out.append("**")
        elif tag in ("em", "i"):
            self._out.append("*")
        elif tag == "a":
            text = "".join(self._link_text).strip()
            self._out.append(f"[{text}]({self._link_href})" if self._link_href else text)
            self._in_link = False
            self._link_href = ""
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        (self._link_text if self._in_link else self._out).append(data)

    @property
    def markdown(self) -> str:
        text = "".join(self._out)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_markdown(html: str) -> str:
    parser = _MarkdownExtractor()
    parser.feed(html or "")
    return parser.markdown


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
