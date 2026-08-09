"""Turning two versions of an article body into something reviewable.

A raw diff of the HTML is technically complete and practically useless: these
bodies are frequently one enormous line, and even split by tag the review is
drowned in attribute noise. What the owner is approving is *what a reader will
see*, so the primary diff is block-level prose — paragraph, heading, list item
— with the HTML available underneath for anyone who wants it.

Blocks are compared on their normalised text so a re-wrapped paragraph or a
changed class attribute doesn't read as a rewrite. That means a change visible
only in markup won't appear here, which is exactly why the asset guards in
`refresh.apply` are enforced separately and not left to the reviewer's eye.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from bs4 import BeautifulSoup

# Elements that read as their own paragraph to a person.
_BLOCKS = (
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "figcaption",
    "td", "th", "pre",
)


@dataclass(frozen=True)
class Block:
    tag: str
    text: str


@dataclass(frozen=True)
class DiffLine:
    kind: str          # "added" | "removed" | "same"
    tag: str
    text: str


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def blocks(html: str | None) -> list[Block]:
    """Readable blocks in document order.

    `script` and `style` are dropped: the GEO step embeds JSON-LD into the
    body, and a regenerated timestamp inside it would otherwise show up as a
    content change on every single refresh.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    for junk in soup.find_all(["script", "style"]):
        junk.decompose()
    out: list[Block] = []
    for element in soup.find_all(_BLOCKS):
        # Skip a block that only wraps other blocks (a <td> holding a <p>):
        # its text would duplicate the child's.
        if element.find(_BLOCKS):
            continue
        text = _normalise(element.get_text(" "))
        if text:
            out.append(Block(tag=element.name, text=text))
    return out


def diff(original: str | None, proposed: str | None) -> list[DiffLine]:
    """Block-level diff, in reading order."""
    left = blocks(original)
    right = blocks(proposed)
    matcher = SequenceMatcher(
        None, [b.text for b in left], [b.text for b in right], autojunk=False
    )
    out: list[DiffLine] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            out.extend(DiffLine("same", b.tag, b.text) for b in left[i1:i2])
        else:
            # Removed before added, so a replacement reads as before/after.
            out.extend(DiffLine("removed", b.tag, b.text) for b in left[i1:i2])
            out.extend(DiffLine("added", b.tag, b.text) for b in right[j1:j2])
    return out


def summarise(original: str | None, proposed: str | None) -> dict:
    """Counts worth showing above the diff, before anyone starts reading."""
    lines = diff(original, proposed)
    left, right = blocks(original), blocks(proposed)
    return {
        "added": sum(1 for line in lines if line.kind == "added"),
        "removed": sum(1 for line in lines if line.kind == "removed"),
        "unchanged": sum(1 for line in lines if line.kind == "same"),
        "words_before": sum(len(b.text.split()) for b in left),
        "words_after": sum(len(b.text.split()) for b in right),
        "blocks_before": len(left),
        "blocks_after": len(right),
    }


def collapse(lines: list[DiffLine], context: int = 1) -> list[DiffLine | None]:
    """Drop long runs of unchanged blocks, keeping `context` either side.

    A None marks an elision, so the template can render a gap rather than
    pretending the removed blocks were never there. Reviewing a 1,500-word
    article where three paragraphs changed should not mean scrolling past the
    other forty.
    """
    keep = [False] * len(lines)
    for index, line in enumerate(lines):
        if line.kind == "same":
            continue
        for offset in range(-context, context + 1):
            neighbour = index + offset
            if 0 <= neighbour < len(lines):
                keep[neighbour] = True

    out: list[DiffLine | None] = []
    elided = False
    for index, line in enumerate(lines):
        if keep[index]:
            out.append(line)
            elided = False
        elif not elided:
            out.append(None)
            elided = True
    return out
