"""QA-outcome routing.

Two layers: (1) the post-QA branch that decides auto-publish vs. Linear-only,
and (2) the Linear workflow-state mapping for the Linear-only path.
"""

import blog_pipeline.graphs.article_graph as ag
from blog_pipeline.graphs.article_graph import (
    _target_state,
    route_after_qa,
    route_after_seo,
)


# ── Linear workflow-state mapping (Linear-only path) ──────────────
def _cfg_states(monkeypatch, review="REVIEW", needs="NEEDS", blocked="BLOCK"):
    monkeypatch.setenv("LINEAR_REVIEW_STATE", review)
    monkeypatch.setenv("LINEAR_NEEDS_WORK_STATE", needs)
    monkeypatch.setenv("LINEAR_BLOCKED_STATE", blocked)
    import blog_pipeline.config as config

    config.get_settings.cache_clear()


def test_block_verdict_routes_to_blocked(monkeypatch):
    _cfg_states(monkeypatch)
    assert _target_state("block", 0.99, 0.75) == "BLOCK"


def test_high_confidence_pass_routes_to_review(monkeypatch):
    _cfg_states(monkeypatch)
    assert _target_state("pass", 0.9, 0.75) == "REVIEW"


def test_low_confidence_pass_routes_to_needs_work(monkeypatch):
    _cfg_states(monkeypatch)
    assert _target_state("pass", 0.5, 0.75) == "NEEDS"


def test_review_verdict_routes_to_needs_work(monkeypatch):
    _cfg_states(monkeypatch)
    assert _target_state("review", 0.99, 0.75) == "NEEDS"


# ── auto-publish vs. Linear-only branch ───────────────────────────
class _FakeSettings:
    def __init__(self, can_autopublish=True, threshold=0.75):
        self.can_autopublish = can_autopublish
        self.confidence_threshold = threshold


def _patch(monkeypatch, can_autopublish, threshold=0.75):
    monkeypatch.setattr(
        ag, "get_settings", lambda: _FakeSettings(can_autopublish, threshold)
    )


def test_confident_pass_with_shopify_publishes(monkeypatch):
    _patch(monkeypatch, can_autopublish=True)
    state = {"qa_report": {"verdict": "pass"}, "confidence": 0.9}
    assert route_after_qa(state) == "publish"


def test_confident_pass_without_shopify_syncs_only(monkeypatch):
    _patch(monkeypatch, can_autopublish=False)
    state = {"qa_report": {"verdict": "pass"}, "confidence": 0.9}
    assert route_after_qa(state) == "sync_linear"


def test_low_confidence_never_publishes(monkeypatch):
    _patch(monkeypatch, can_autopublish=True)
    state = {"qa_report": {"verdict": "pass"}, "confidence": 0.5}
    assert route_after_qa(state) == "sync_linear"


def test_block_verdict_never_publishes(monkeypatch):
    _patch(monkeypatch, can_autopublish=True)
    state = {"qa_report": {"verdict": "block"}, "confidence": 0.99}
    assert route_after_qa(state) == "sync_linear"


# ── SEO revision loop (capped at one pass) ────────────────────────
class _SeoSettings:
    seo_min_score = 85


def _patch_seo(monkeypatch):
    monkeypatch.setattr(ag, "get_settings", lambda: _SeoSettings())


def test_low_score_first_pass_revises(monkeypatch):
    _patch_seo(monkeypatch)
    assert route_after_seo({"seo_score": 70.0, "revision_count": 0}) == "revise"


def test_low_score_after_one_revision_moves_on(monkeypatch):
    _patch_seo(monkeypatch)
    assert route_after_seo({"seo_score": 70.0, "revision_count": 1}) == "images"


def test_passing_score_skips_revision(monkeypatch):
    _patch_seo(monkeypatch)
    assert route_after_seo({"seo_score": 92.0, "revision_count": 0}) == "images"
