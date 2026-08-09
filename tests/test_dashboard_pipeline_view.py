"""Why an article isn't live.

The whole value of this view is that the reason it gives is the *real* one.
A page that says "SEO score 61" next to a stuck article invites you to go and
improve the SEO score, which would change nothing — the publish gate never
looks at it. These tests pin the reason to what
`article_graph.route_after_qa` actually does: Shopify configured and enabled,
QA verdict "pass", and confidence at or above the threshold.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from dashboard.reporting import _blocked_reason

TODAY = date(2026, 8, 9)


class _Article:
    """Only the fields the classifier reads."""

    def __init__(self, **kw):
        self.failure_reason = kw.get("failure_reason")
        self.qa_report = kw.get("qa_report", {"verdict": "pass"})
        self.qa_confidence_score = kw.get("confidence", 1.0)
        self.shopify_article_id = kw.get("shopify_article_id")
        self.seo_score = kw.get("seo_score", 90.0)
        self.title = kw.get("title", "An article")
        self.updated_at = datetime(2026, 8, 1)


def _stage(**kw) -> str:
    return _blocked_reason(_Article(**kw), confidence_threshold=0.75)[0]


def test_an_article_in_shopify_is_a_draft_waiting_on_a_click():
    """SHOPIFY_PUBLISH_LIVE=false creates confident articles hidden on
    purpose. This is the cheapest pile to clear and must not be presented as
    a failure — the writing is done and it passed review."""
    stage, reason, action = _blocked_reason(
        _Article(shopify_article_id="gid://shopify/Article/1"),
        confidence_threshold=0.75,
    )

    assert stage == "shopify_draft"
    assert "hidden draft" in reason
    assert "Publish" in action


def test_a_review_verdict_is_a_qa_hold():
    stage, reason, _ = _blocked_reason(
        _Article(qa_report={"verdict": "review"}), confidence_threshold=0.75
    )

    assert stage == "held"
    assert "review" in reason


def test_low_confidence_is_a_qa_hold_even_on_a_pass_verdict():
    """Both conditions gate publishing independently, so a 'pass' that didn't
    clear the confidence bar is still held — and the reason has to say which
    of the two it was."""
    stage, reason, _ = _blocked_reason(
        _Article(qa_report={"verdict": "pass"}, confidence=0.5),
        confidence_threshold=0.75,
    )

    assert stage == "held"
    assert "0.50" in reason and "0.75" in reason


def test_both_problems_are_named_not_just_the_first():
    _, reason, _ = _blocked_reason(
        _Article(qa_report={"verdict": "review"}, confidence=0.5),
        confidence_threshold=0.75,
    )

    assert "verdict" in reason and "confidence" in reason


def test_a_low_seo_score_is_never_given_as_the_reason():
    """The publish gate does not look at the SEO score — it triggers one
    revision pass and nothing more. Naming it here would send the owner to
    fix a thing that would not release the article. A real article scored
    60.6 and was held for its QA verdict, not for that."""
    _, reason, action = _blocked_reason(
        _Article(seo_score=60.6, qa_report={"verdict": "review"}),
        confidence_threshold=0.75,
    )

    assert "SEO" not in reason and "SEO" not in action
    assert "seo" not in reason.lower()


def test_a_failed_run_reports_its_own_reason():
    stage, reason, _ = _blocked_reason(
        _Article(failure_reason="Shopify rejected the handle"),
        confidence_threshold=0.75,
    )

    assert stage == "held"
    assert "Shopify rejected the handle" in reason


def test_passing_everything_with_no_shopify_article_is_stranded():
    """Distinct from a QA hold and from a draft: it cleared every gate and
    still isn't anywhere. Seen for real on one article. Reading it as "held"
    would send the owner to re-review something that already passed."""
    stage, reason, action = _blocked_reason(
        _Article(qa_report={"verdict": "pass"}, confidence=1.0),
        confidence_threshold=0.75,
    )

    assert stage == "stranded"
    assert "publish step" in reason
    assert "Linear" in action


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"shopify_article_id": "gid://1", "qa_report": {"verdict": "review"}},
         "shopify_draft"),
        ({"failure_reason": "boom", "shopify_article_id": "gid://1"}, "held"),
    ],
)
def test_precedence_between_overlapping_states(kwargs, expected):
    """An article can satisfy more than one description at once. A recorded
    failure outranks everything (it's the most specific thing known), and
    otherwise existing in Shopify outranks the QA verdict that put it there —
    because at that point the click is what's left to do."""
    assert _stage(**kwargs) == expected


def test_the_view_survives_an_unreadable_pipeline_database(monkeypatch):
    """The Blog page reads the pipeline's database, which is a separate file
    that can be missing or mid-migration. That must be a message on the page,
    not a 500 that takes the performance tables down with it."""
    import blog_pipeline.db.session as pipeline_session

    from dashboard import reporting

    monkeypatch.setattr(
        pipeline_session, "get_session",
        lambda: (_ for _ in ()).throw(RuntimeError("database is locked")),
    )

    data = reporting.content_pipeline(today=TODAY)

    assert data["available"] is False
    assert "database is locked" in data["error"]


def test_an_empty_queue_is_visible_as_zero_not_absent(dashboard_db):
    """"Nothing is queued" is the single most actionable fact this view can
    report — it means no article will be written at all. It has to survive as
    a zero rather than an empty list nobody renders."""
    from blog_pipeline.db.session import init_db as init_pipeline

    from dashboard import reporting

    # conftest points DATABASE_URL at a temp file but doesn't build the
    # pipeline's schema in it; this view reads those tables directly.
    init_pipeline()

    data = reporting.content_pipeline(today=TODAY)

    assert data["available"] is True
    assert data["counts"]["queued"] == 0
    assert data["upcoming"] == []
