"""Proposing "their product is our product", for a human to decide.

Matching flooring across retailers is genuinely hard and this does not
pretend otherwise. The same board is sold under different series names by
different shops; the discriminators that actually identify it — thickness in
mm, wear layer in mil, AC rating, plank width — are buried in free-text
titles in no consistent order. An automatic matcher would be confidently
wrong often enough to poison every price comparison downstream, and a wrong
price comparison is worse than none: it produces an alert saying you're
being undercut on a product that isn't the same product.

So: the machine proposes and shows its working, and the owner confirms once.
A confirmed match persists. A rejected one persists too — deleting a
rejection just means proposing it again tomorrow.

PLAN.md originally called for fastembed title embeddings here. That was
dropped deliberately: fastembed pulls ~45MB of onnxruntime into a serverless
bundle, and for this problem it would mostly be an expensive way to notice
that two titles both say "laminate". The attributes below are what actually
distinguish flooring, and they're cheap to extract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Anything below this isn't worth a human's attention — the review queue is
#: only useful if most of what's in it is right.
MIN_SCORE = 0.45

#: Words that appear in nearly every flooring title and so distinguish
#: nothing. Kept small on purpose: an over-eager stop list throws away the
#: series names that do the real work.
_NOISE = frozenset(
    """
    flooring floor floors plank planks board boards tile tiles
    the and for with in of a an by mm sqft sq ft
    colour color series collection
    """.split()
)

_WORD = re.compile(r"[a-z0-9.]+")
#: "12mm", "12 mm", "5.5mm" — thickness is the single strongest signal two
#: flooring products are the same thing.
_THICKNESS = re.compile(r"(\d+(?:\.\d+)?)\s*mm\b")
#: "20mil", "12 mil" — wear layer, the equivalent for vinyl.
_WEARLAYER = re.compile(r"(\d+(?:\.\d+)?)\s*mil\b")
#: "AC4", "AC 5" — abrasion class.
_AC = re.compile(r"\bac\s?([1-6])\b")


@dataclass(frozen=True)
class Signature:
    """The identifying bits of a product title."""

    words: frozenset[str]
    thickness: float | None
    wear_layer: float | None
    ac: str | None
    vendor: str | None


def signature(title: str, vendor: str | None = None) -> Signature:
    text = (title or "").lower()
    thickness = _THICKNESS.search(text)
    wear = _WEARLAYER.search(text)
    ac = _AC.search(text)
    words = {
        w
        for w in _WORD.findall(text)
        if w not in _NOISE and len(w) > 2 and not w.isdigit()
    }
    return Signature(
        words=frozenset(words),
        thickness=float(thickness.group(1)) if thickness else None,
        wear_layer=float(wear.group(1)) if wear else None,
        ac=ac.group(1) if ac else None,
        vendor=(vendor or "").strip().lower() or None,
    )


def score(ours: Signature, theirs: Signature) -> tuple[float, list[str]]:
    """0..1 and the reasons, for the review queue to show its working.

    Not a probability. It exists to sort the queue so the obvious matches get
    confirmed first and the marginal ones are visibly marginal.
    """
    reasons: list[str] = []

    overlap = ours.words & theirs.words
    union = ours.words | theirs.words
    jaccard = len(overlap) / len(union) if union else 0.0
    total = jaccard * 0.5
    if overlap:
        shown = ", ".join(sorted(overlap)[:4])
        reasons.append(f"shared words: {shown}")

    # Thickness is the strongest single discriminator, and a *mismatch* is
    # near-conclusive the other way: 8mm and 12mm laminate are not the same
    # product however similar the names.
    if ours.thickness and theirs.thickness:
        if abs(ours.thickness - theirs.thickness) < 0.01:
            total += 0.3
            reasons.append(f"same thickness ({ours.thickness:g}mm)")
        else:
            total -= 0.4
            reasons.append(
                f"different thickness ({ours.thickness:g}mm vs "
                f"{theirs.thickness:g}mm)"
            )

    if ours.wear_layer and theirs.wear_layer:
        if abs(ours.wear_layer - theirs.wear_layer) < 0.01:
            total += 0.15
            reasons.append(f"same wear layer ({ours.wear_layer:g}mil)")
        else:
            total -= 0.2
            reasons.append("different wear layer")

    if ours.ac and theirs.ac:
        if ours.ac == theirs.ac:
            total += 0.1
            reasons.append(f"same AC rating (AC{ours.ac})")
        else:
            total -= 0.15
            reasons.append("different AC rating")

    # Same brand across two retailers is a strong hint; different brands is
    # not evidence against, because retailers rename the same product.
    if ours.vendor and theirs.vendor and ours.vendor == theirs.vendor:
        total += 0.15
        reasons.append(f"same brand ({ours.vendor})")

    return max(0.0, min(1.0, total)), reasons


def propose(
    our_products: list, their_products: list, *, limit_per_product: int = 1
) -> list[tuple[int, int, float, str]]:
    """Best candidate match(es) for each of their products.

    Returns `(their_id, our_id, score, reason)`. Only their best candidate is
    proposed rather than every pair above the threshold — a review queue with
    six near-identical options for one product is a queue nobody finishes.

    Blocked on the first shared word to keep this from being O(n*m) across
    two multi-thousand-product catalogues: two flooring products with no
    significant word in common are not the same product, and checking that
    with a set index is far cheaper than scoring the pair.
    """
    ours_sigs = [(p.id, signature(p.title, getattr(p, "vendor", None)))
                 for p in our_products]

    index: dict[str, list[int]] = {}
    for pos, (_pid, sig) in enumerate(ours_sigs):
        for word in sig.words:
            index.setdefault(word, []).append(pos)

    out: list[tuple[int, int, float, str]] = []
    for theirs in their_products:
        their_sig = signature(theirs.title, getattr(theirs, "vendor", None))
        candidates: set[int] = set()
        for word in their_sig.words:
            candidates.update(index.get(word, ()))
        if not candidates:
            continue

        scored: list[tuple[float, int, str]] = []
        for pos in candidates:
            our_id, our_sig = ours_sigs[pos]
            value, reasons = score(our_sig, their_sig)
            if value >= MIN_SCORE:
                scored.append((value, our_id, "; ".join(reasons)[:400]))
        scored.sort(reverse=True)
        for value, our_id, reason in scored[:limit_per_product]:
            out.append((theirs.id, our_id, round(value, 3), reason))
    return out
