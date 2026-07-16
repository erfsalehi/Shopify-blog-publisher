"""GA4 AI-referral tracking.

Search Console covers Google Search only, so this is the sole direct evidence
that being cited by an AI assistant is worth anything. The risk here is
over-claiming: counting an ambiguous referrer as AI would silently inflate the
one number the whole GEO effort is judged by.
"""

from datetime import date, timedelta

import pytest

from blog_pipeline.db import AiReferral, Article, get_session, init_db
from blog_pipeline.db.models import ArticleStatus, TopicSource
from blog_pipeline.performance import _path_of, ai_referral_summary, sync_ai_referrals
from blog_pipeline.tools.analytics import is_ai_source

W_END = date(2026, 7, 1)


class _FakeGA4:
    def __init__(self, rows=None, enabled=True):
        self._rows, self.enabled = rows or [], enabled

    def run_report(self, *, dimensions, metrics, start_date, end_date, limit=50000):
        return self._rows


def _ga_row(source, landing, sessions=10, users=8):
    return {"dimensions": [source, landing], "metrics": [str(sessions), str(users)]}


@pytest.fixture
def ga4(monkeypatch):
    init_db()
    holder = {"client": _FakeGA4()}
    monkeypatch.setattr(
        "blog_pipeline.performance.AnalyticsClient", lambda: holder["client"]
    )
    monkeypatch.setattr(
        "blog_pipeline.performance.default_window",
        lambda days=90: (W_END - timedelta(days=days), W_END),
    )
    return holder


# ── which referrers count as AI ─────────────────────────────────


@pytest.mark.parametrize(
    "source",
    ["chatgpt.com", "chat.openai.com", "perplexity.ai", "www.perplexity.ai",
     "claude.ai", "copilot.microsoft.com", "gemini.google.com"],
)
def test_known_assistants_are_recognised(source):
    assert is_ai_source(source) is True


@pytest.mark.parametrize(
    "source",
    ["google.com", "bing.com", "facebook.com", "(direct)", "", None,
     "duckduckgo.com", "yahoo.com"],
)
def test_ordinary_traffic_is_not_counted_as_ai(source):
    """google.com and bing.com serve both ordinary search and AI answers with
    no way to tell them apart from a referrer. Counting them would quietly
    credit AI for organic search — better to under-report."""
    assert is_ai_source(source) is False


def test_lookalike_domains_are_not_matched():
    """Substring matching would let anyone spoof the metric with a hostname."""
    assert is_ai_source("notchatgpt.com.evil.example") is False
    assert is_ai_source("chatgpt.com.phish.example") is False


def test_matching_is_case_and_www_insensitive():
    assert is_ai_source("ChatGPT.com") is True
    assert is_ai_source("www.chatgpt.com") is True


# ── landing page -> article ─────────────────────────────────────


def test_ga4_paths_match_stored_absolute_urls():
    """GA4 reports "/blogs/news/x"; articles store the absolute URL. Without
    reducing both to a path, nothing would ever join."""
    assert _path_of("https://drflooring.ca/blogs/news/x") == _path_of("/blogs/news/x")


def test_different_paths_still_differ():
    assert _path_of("/blogs/news/a") != _path_of("/blogs/news/b")


# ── sync ────────────────────────────────────────────────────────


def test_unconfigured_ga4_is_a_no_op(ga4):
    ga4["client"] = _FakeGA4(enabled=False)
    assert sync_ai_referrals()["enabled"] is False


def test_only_ai_rows_are_stored(ga4):
    ga4["client"] = _FakeGA4(rows=[
        _ga_row("chatgpt.com", "/blogs/news/post-1", sessions=12),
        _ga_row("google.com", "/blogs/news/post-1", sessions=5000),
        _ga_row("perplexity.ai", "/blogs/news/post-1", sessions=3),
    ])
    result = sync_ai_referrals()

    assert result["rows_scanned"] == 3
    assert result["ai_rows"] == 2
    assert result["ai_sessions"] == 15  # google's 5000 excluded
    with get_session() as s:
        assert {r.source for r in s.query(AiReferral).all()} == {
            "chatgpt.com", "perplexity.ai"
        }


def test_referrals_join_to_the_article_that_was_landed_on(ga4):
    with get_session() as s:
        a = Article(
            topic="Post 1", title="Post 1", status=ArticleStatus.published,
            topic_source=TopicSource.imported,
            shopify_url="https://drflooring.ca/blogs/news/post-1",
        )
        s.add(a)
        s.flush()
        article_id = a.id

    ga4["client"] = _FakeGA4(rows=[_ga_row("chatgpt.com", "/blogs/news/post-1")])
    sync_ai_referrals()

    with get_session() as s:
        assert s.query(AiReferral).one().article_id == article_id


def test_a_landing_page_with_no_article_still_counts(ga4):
    """An AI sending someone to a product page is still an AI referral."""
    ga4["client"] = _FakeGA4(rows=[_ga_row("chatgpt.com", "/products/underlay")])
    assert sync_ai_referrals()["ai_sessions"] == 10
    with get_session() as s:
        assert s.query(AiReferral).one().article_id is None


def test_resync_replaces_rather_than_doubles(ga4):
    ga4["client"] = _FakeGA4(rows=[_ga_row("chatgpt.com", "/blogs/news/x")])
    sync_ai_referrals()
    sync_ai_referrals()
    with get_session() as s:
        assert s.query(AiReferral).count() == 1


def test_dry_run_stores_nothing(ga4):
    ga4["client"] = _FakeGA4(rows=[_ga_row("chatgpt.com", "/blogs/news/x")])
    assert sync_ai_referrals(dry_run=True)["ai_sessions"] == 10
    with get_session() as s:
        assert s.query(AiReferral).count() == 0


def test_no_ai_traffic_is_a_finding_not_an_error(ga4):
    """Zero is the expected answer for most sites today and must not look like
    a failure."""
    ga4["client"] = _FakeGA4(rows=[_ga_row("google.com", "/blogs/news/x")])
    result = sync_ai_referrals()
    assert result["enabled"] is True
    assert result["ai_sessions"] == 0


# ── summary ─────────────────────────────────────────────────────


def test_summary_unavailable_before_any_sync(ga4):
    assert ai_referral_summary()["available"] is False


def test_summary_aggregates_by_source(ga4):
    ga4["client"] = _FakeGA4(rows=[
        _ga_row("chatgpt.com", "/a", sessions=10),
        _ga_row("chatgpt.com", "/b", sessions=5),
        _ga_row("perplexity.ai", "/a", sessions=3),
    ])
    sync_ai_referrals()

    summary = ai_referral_summary()
    assert summary["total_sessions"] == 18
    assert summary["sources"][0] == ("chatgpt.com", 15)
