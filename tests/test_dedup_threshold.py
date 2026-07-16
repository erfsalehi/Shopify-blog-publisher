"""The semantic dedup threshold, pinned against real measured pairs.

A wrongly-rejected topic is the expensive failure mode and the invisible one:
the article is never written, and the only trace is a line in a JSON log. A
wrongly-kept one shows up as a near-duplicate in Linear and costs a draft.

The numbers below are measured against the live corpus (71 flooring articles),
not chosen by feel. bge-small's range is compressed and everything inside one
niche scores high, so a threshold that looks conservative in the abstract can
sit inside the legitimate band — which is exactly what 0.82 did.

These call the real embedder, so they need the fastembed model. Skipped when
it can't load rather than failing the suite offline.
"""

import pytest

from blog_pipeline.dedup import SEMANTIC_THRESHOLD, _cosine, _embed, is_duplicate


@pytest.fixture(scope="module")
def embedder():
    try:
        _embed(["warmup"])
    except Exception as e:  # pragma: no cover - offline / model unavailable
        pytest.skip(f"fastembed unavailable: {e}")


def _sim(a: str, b: str) -> float:
    v = _embed([a, b])
    return _cosine(v[0], v[1])


# (candidate, existing, is_really_a_duplicate)
_REAL_PAIRS = [
    # The regression: rejected at 0.840 under the old 0.82 threshold. This is
    # the site's top striking-distance query — 3,901 impressions at position
    # 9.1 with zero clicks — killed over shared phrasing.
    (
        "How to Choose the Best Flooring Underlayment for Langley Homes",
        "Elevating Your Space: How to Choose the Ideal Flooring Store in Langley",
        False,
    ),
    # Survived the old threshold by 0.005. Same class of near-miss.
    (
        "Engineered Hardwood vs. Solid Hardwood: A Canadian Climate Guide",
        "All You Need To Know: Engineered Wood Flooring",
        False,
    ),
    (
        "Foam vs. Felt Underlay: Which is Best for Your Floor?",
        "Flooring Underlayment: A Comprehensive Guide",
        False,
    ),
    # Genuine duplicates — must stay rejected.
    (
        "What is SPC Flooring? A Simple Guide to Stone Plastic Composite",
        "The Ultimate Guide to SPC Flooring",
        True,
    ),
    (
        "Loose Lay Vinyl Flooring: Pros, Cons, and Installation Tips for DIYers",
        "Exploring the Pros and Cons of Loose Lay Vinyl Flooring: Everything You Need to Know",
        True,
    ),
    (
        "Eco-Friendly Flooring Options for Canadian Homes",
        "Exploring Eco-Friendly Flooring Solutions for a Sustainable Home",
        True,
    ),
    (
        "Herringbone Flooring Trends: Is it Right for Your Space?",
        "Upgrade Your Floors with Herringbone!",
        True,
    ),
]


@pytest.mark.parametrize(
    "candidate,existing,is_dup",
    _REAL_PAIRS,
    ids=lambda v: (v[:34] if isinstance(v, str) else str(v)),
)
def test_real_pairs_land_on_the_right_side(embedder, candidate, existing, is_dup):
    hit = is_duplicate(candidate, [existing])
    assert hit.is_dup is is_dup, (
        f"{_sim(candidate, existing):.3f} vs threshold {SEMANTIC_THRESHOLD}"
    )


def test_the_threshold_sits_between_the_two_bands(embedder):
    """The real separation: new topics top out ~0.840, duplicates start ~0.870.
    A threshold outside that gap silently mis-sorts one band or the other."""
    new = [_sim(c, e) for c, e, dup in _REAL_PAIRS if not dup]
    dups = [_sim(c, e) for c, e, dup in _REAL_PAIRS if dup]

    assert max(new) < SEMANTIC_THRESHOLD < min(dups), (
        f"threshold {SEMANTIC_THRESHOLD} outside the measured gap: "
        f"new topics peak at {max(new):.3f}, duplicates start at {min(dups):.3f}"
    )


def test_unrelated_topics_are_nowhere_near_the_threshold(embedder):
    """Sanity on the baseline: inside one niche even unrelated topics score
    ~0.71, which is why the threshold can't be reasoned about in the abstract.
    """
    assert _sim("Choosing the Best Baseboards for Your New Floors",
                "All You Need to Know: Linoleum Flooring") < 0.80
