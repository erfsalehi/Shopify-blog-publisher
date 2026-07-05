from blog_pipeline.dedup import filter_new_topics, is_duplicate
from blog_pipeline.utils import keyword_overlap


def test_keyword_overlap_identical():
    assert keyword_overlap("best running shoes", "best running shoes") == 1.0


def test_keyword_overlap_disjoint():
    assert keyword_overlap("running shoes", "kitchen knives") == 0.0


def test_exact_duplicate_detected_without_semantic():
    existing = ["How to choose the best running shoes"]
    hit = is_duplicate(
        "How to choose the best running shoes", existing, use_semantic=False
    )
    assert hit.is_dup
    assert hit.method == "keyword_overlap"


def test_unique_topic_not_flagged_without_semantic():
    existing = ["Kitchen knife maintenance guide"]
    hit = is_duplicate("Trail running nutrition tips", existing, use_semantic=False)
    assert not hit.is_dup


def test_filter_new_topics_dedupes_within_batch():
    # Two near-identical candidates: the second should be rejected against the
    # first even though neither is in `existing`.
    candidates = [
        "Best running shoes for beginners",
        "Best running shoes for beginners guide",
        "How to clean hiking boots",
    ]
    kept, rejected = filter_new_topics(candidates, [], use_semantic=False)
    assert "How to clean hiking boots" in kept
    assert len(kept) + len(rejected) == 3
    assert len(rejected) >= 1
