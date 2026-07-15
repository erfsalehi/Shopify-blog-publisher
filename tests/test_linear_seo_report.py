"""The Linear issue must explain the SEO score, not just state it.

score_seo computes a full metrics dict and uses it to decide whether an
article clears seo_min_score, but only the headline number used to reach the
issue — leaving a gated article with no indication of which lever missed.
"""

import pytest

from blog_pipeline.graphs.article_graph import _build_description, _seo_metrics_table

_METRICS = {
    "word_count": 1423,
    "kw_in_title": True,
    "kw_in_intro": False,
    "keyword_density": 0.0091,
    "secondary_coverage": 0.5,
    "h2_count": 6,
    "flesch_reading_ease": 47.2,
    "meta_description_length": 108,
    "internal_links": 1,
    "has_pull_quote": False,
    "source_count": 2,
    "chunk_compliant_sections": "4/6",
}


def _state(**over):
    state = {
        "topic": "engineered hardwood",
        "title": "Engineered Hardwood",
        "outline": {"primary_keyword": "engineered hardwood", "secondary_keywords": []},
        "body_html": "<p>body</p>",
        "seo_metrics": dict(_METRICS),
        "seo_score": 78.5,
    }
    state.update(over)
    return state


def test_every_computed_metric_reaches_the_issue():
    body = _build_description(_state())
    for label in (
        "Word count",
        "Primary keyword in title",
        "Primary keyword in first 100 words",
        "Keyword density",
        "Secondary keywords covered",
        "H2 sections",
        "Reading ease (Flesch)",
        "Meta description",
        "Internal links",
        "Pull quote (GEO)",
        "Named sources (GEO)",
        "Sections in the 150-400 word band (GEO)",
    ):
        assert label in body, f"{label!r} missing from the Linear issue"


def test_values_are_rendered_for_humans_not_as_raw_floats():
    body = _build_description(_state())
    assert "| Keyword density | 0.91% |" in body       # not 0.0091
    assert "| Secondary keywords covered | 50% |" in body  # not 0.5
    assert "| Word count | 1,423 |" in body
    assert "| Primary keyword in first 100 words | no |" in body  # not False


def test_score_carries_the_gate_that_makes_it_meaningful(monkeypatch):
    monkeypatch.setenv("SEO_MIN_SCORE", "85")
    import blog_pipeline.config as config

    config.get_settings.cache_clear()
    try:
        assert "**SEO score:** 78.5/100 (passes at 85)" in _build_description(_state())
    finally:
        config.get_settings.cache_clear()


def test_falsy_metrics_are_reported_not_skipped():
    """0 and False are real findings — 'no internal links' is the whole point."""
    body = _build_description(
        _state(seo_metrics={"internal_links": 0, "has_pull_quote": False, "source_count": 0})
    )
    assert "| Internal links | 0 |" in body
    assert "| Pull quote (GEO) | no |" in body
    assert "| Named sources (GEO) | 0 |" in body


def test_no_metrics_yields_no_empty_table():
    assert _seo_metrics_table({}) == []
    body = _build_description(_state(seo_metrics={}))
    assert "| Metric | Value | Target |" not in body


@pytest.mark.parametrize("bad", [None, "n/a", object()])
def test_an_unformattable_value_cannot_take_down_the_issue(bad):
    """A formatter blowing up would lose the entire article write-up over one
    bad cell, so rows degrade to a repr instead of raising."""
    rows = _seo_metrics_table({"keyword_density": bad, "word_count": 900})
    assert "| Word count | 900 |" in "\n".join(rows)
