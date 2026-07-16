"""Search Console sync and the two questions it answers.

The joins here fail silently by nature: a URL mismatch or an unconfigured
property both present as "no data", which is indistinguishable from a site
with no traffic. Most of these tests exist to make that failure loud.
"""

from datetime import date, timedelta

import pytest

from blog_pipeline.db import Article, SearchPerformance, get_session, init_db
from blog_pipeline.db.models import ArticleStatus, TopicSource
from blog_pipeline.performance import (
    _normalize,
    decaying_articles,
    site_summary,
    striking_distance_queries,
    sync_performance,
    top_pages,
)

W1_END = date(2026, 4, 1)
W2_END = date(2026, 7, 1)


def _row(keys, clicks=1, impressions=100, ctr=0.01, position=12.0):
    return {
        "keys": keys, "clicks": clicks, "impressions": impressions,
        "ctr": ctr, "position": position,
    }


class _FakeGSC:
    def __init__(self, pages=None, queries=None, enabled=True):
        self._pages, self._queries, self.enabled = pages or [], queries or [], enabled

    def query(self, *, dimensions, start_date, end_date, row_limit=25000):
        return self._pages if dimensions == ["page"] else self._queries


@pytest.fixture
def gsc(monkeypatch):
    init_db()
    holder = {"client": _FakeGSC()}
    monkeypatch.setattr(
        "blog_pipeline.performance.SearchConsoleClient", lambda: holder["client"]
    )
    monkeypatch.setattr(
        "blog_pipeline.performance.default_window",
        lambda days=90: (W2_END - timedelta(days=days), W2_END),
    )
    return holder


def _article(url="https://drflooring.ca/blogs/news/post-1", title="Post 1"):
    with get_session() as s:
        a = Article(
            topic=title, title=title, status=ArticleStatus.published,
            topic_source=TopicSource.imported, shopify_url=url,
            shopify_article_id="gid://shopify/Article/1",
        )
        s.add(a)
        s.flush()
        return a.id


# ── URL matching ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "a,b",
    [
        ("https://drflooring.ca/blogs/news/x", "http://drflooring.ca/blogs/news/x"),
        ("https://drflooring.ca/blogs/news/x", "https://www.drflooring.ca/blogs/news/x"),
        ("https://drflooring.ca/blogs/news/x/", "https://drflooring.ca/blogs/news/x"),
        ("https://DRFlooring.ca/Blogs/News/X", "https://drflooring.ca/blogs/news/x"),
    ],
)
def test_urls_match_across_scheme_www_slash_and_case(a, b):
    """Search Console reports the canonical URL, which needn't match ours
    character-for-character — and a join matching nothing looks like no
    traffic rather than a bug."""
    assert _normalize(a) == _normalize(b)


def test_different_pages_do_not_collide():
    assert _normalize("https://x.ca/a") != _normalize("https://x.ca/b")


# ── sync ────────────────────────────────────────────────────────


def test_unconfigured_search_console_is_a_no_op_not_a_crash(gsc):
    gsc["client"] = _FakeGSC(enabled=False)
    assert sync_performance()["enabled"] is False


def test_sync_stores_pages_and_queries_and_joins_to_articles(gsc):
    article_id = _article()
    gsc["client"] = _FakeGSC(
        pages=[_row(["https://drflooring.ca/blogs/news/post-1"], impressions=500)],
        queries=[_row(["hardwood flooring langley"], impressions=300)],
    )
    result = sync_performance(compare=False)

    assert result["pages"] == 1 and result["queries"] == 1
    assert result["matched"] == 1
    with get_session() as s:
        page_row = s.query(SearchPerformance).filter(
            SearchPerformance.page.isnot(None)
        ).one()
        assert page_row.article_id == article_id
        assert page_row.impressions == 500


def test_sync_also_pulls_the_preceding_window(gsc):
    """Decay needs two windows. Fetching only the current one would leave
    consecutive weekly syncs overlapping ~92%, whose delta is noise — the
    trend wouldn't be usable for months."""
    _article()
    gsc["client"] = _FakeGSC(pages=[_row(["https://drflooring.ca/blogs/news/post-1"])])
    result = sync_performance(days=90)

    assert result["compared_to"] is not None
    with get_session() as s:
        windows = {r.period_end for r in s.query(SearchPerformance).all()}
    assert len(windows) == 2
    # Adjacent and non-overlapping: one ends exactly where the other starts.
    assert (max(windows) - min(windows)).days == 90


def test_a_page_matching_no_article_still_stores_with_a_null_article(gsc):
    """Product and collection pages have no Article row; their traffic is
    still real and worth keeping."""
    gsc["client"] = _FakeGSC(pages=[_row(["https://drflooring.ca/collections/vinyl"])])
    result = sync_performance(compare=False)

    assert result["matched"] == 0
    with get_session() as s:
        assert s.query(SearchPerformance).one().article_id is None


def test_resyncing_the_same_window_replaces_rather_than_doubles(gsc):
    gsc["client"] = _FakeGSC(queries=[_row(["x"], impressions=100)])
    sync_performance(compare=False)
    sync_performance(compare=False)

    with get_session() as s:
        assert s.query(SearchPerformance).count() == 1


def test_old_windows_are_pruned(gsc, monkeypatch):
    """One sync of the real site is ~31k rows and a weekly cron adds two
    windows a run — ~240MB/year against a 500MB free tier. Only the two most
    recent windows are ever read, so the rest is pure cost."""
    for i, end in enumerate([date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)]):
        _add_query(f"old-{i}", 100, 15.0, period_end=end)

    gsc["client"] = _FakeGSC(queries=[_row(["current"])])
    result = sync_performance(compare=False, retain_windows=2)

    assert result["pruned_rows"] > 0
    with get_session() as s:
        remaining = {r.period_end for r in s.query(SearchPerformance).all()}
    assert len(remaining) == 2
    assert W2_END in remaining  # this run's window always survives


def test_retention_keeps_enough_for_a_decay_comparison(gsc):
    """Pruning must never eat the second window — that would silently break
    decay detection, which reads exactly two."""
    _article()
    gsc["client"] = _FakeGSC(pages=[_row(["https://drflooring.ca/blogs/news/post-1"])])
    sync_performance(days=90, retain_windows=4)

    with get_session() as s:
        assert len({r.period_end for r in s.query(SearchPerformance).all()}) == 2


def test_pruning_can_be_disabled(gsc):
    _add_query("ancient", 100, 15.0, period_end=date(2020, 1, 1))
    gsc["client"] = _FakeGSC(queries=[_row(["current"])])
    sync_performance(compare=False, retain_windows=0)

    with get_session() as s:
        assert len({r.period_end for r in s.query(SearchPerformance).all()}) == 2


def test_dry_run_stores_nothing(gsc):
    gsc["client"] = _FakeGSC(queries=[_row(["x"])])
    assert sync_performance(compare=False, dry_run=True)["queries"] == 1
    with get_session() as s:
        assert s.query(SearchPerformance).count() == 0


# ── striking distance ───────────────────────────────────────────


def _add_query(query, impressions, position, period_end=W2_END):
    with get_session() as s:
        s.add(
            SearchPerformance(
                query=query, impressions=impressions, position=position, clicks=1,
                period_start=period_end - timedelta(days=90), period_end=period_end,
            )
        )


def test_striking_distance_finds_page_two_terms_with_real_demand(gsc):
    _add_query("laminate flooring langley", 800, 14.0)
    rows = striking_distance_queries()

    assert [r["query"] for r in rows] == ["laminate flooring langley"]


def test_terms_already_winning_are_not_striking_distance(gsc):
    """Position 2 needs no article — it already gets the click."""
    _add_query("already ranking", 900, 2.0)
    assert striking_distance_queries() == []


def test_terms_too_far_back_are_excluded(gsc):
    """Position 60 won't be closed by one article."""
    _add_query("hopeless", 900, 60.0)
    assert striking_distance_queries() == []


def test_noise_below_the_impression_floor_is_excluded(gsc):
    _add_query("barely searched", 3, 15.0)
    assert striking_distance_queries(min_impressions=50) == []


def test_striking_distance_is_ordered_by_traffic_on_the_table(gsc):
    _add_query("small", 100, 15.0)
    _add_query("big", 5000, 15.0)
    assert [r["query"] for r in striking_distance_queries()] == ["big", "small"]


def test_striking_distance_uses_only_the_latest_window(gsc):
    _add_query("stale", 5000, 15.0, period_end=W1_END)
    _add_query("fresh", 100, 15.0, period_end=W2_END)
    assert [r["query"] for r in striking_distance_queries()] == ["fresh"]


def test_no_data_yields_no_striking_distance(gsc):
    assert striking_distance_queries() == []


# ── decay ───────────────────────────────────────────────────────


def _add_page(article_id, impressions, period_end):
    with get_session() as s:
        s.add(
            SearchPerformance(
                article_id=article_id, page=f"https://x.ca/{article_id}",
                impressions=impressions, position=10.0, clicks=1,
                period_start=period_end - timedelta(days=90), period_end=period_end,
            )
        )


def test_decay_needs_two_windows_to_mean_anything(gsc):
    article_id = _article()
    _add_page(article_id, 500, W2_END)
    assert decaying_articles() == []  # one window is not a trend


def test_decay_reports_the_drop(gsc):
    article_id = _article()
    _add_page(article_id, 1000, W1_END)
    _add_page(article_id, 600, W2_END)

    rows = decaying_articles()
    assert len(rows) == 1
    assert rows[0]["change_pct"] == -40.0


def test_a_growing_article_is_not_decaying(gsc):
    article_id = _article()
    _add_page(article_id, 100, W1_END)
    _add_page(article_id, 900, W2_END)
    assert decaying_articles() == []


def test_worst_decay_comes_first(gsc):
    a = _article(url="https://x.ca/a", title="A")
    b = _article(url="https://x.ca/b", title="B")
    _add_page(a, 1000, W1_END)
    _add_page(a, 900, W2_END)   # lost 100
    _add_page(b, 1000, W1_END)
    _add_page(b, 200, W2_END)   # lost 800

    assert [r["article_id"] for r in decaying_articles()] == [b, a]


def test_ranked_by_traffic_lost_not_percentage(gsc):
    """The real case this got wrong: ranking by percent put an article that
    fell 235->1 (-99.6%, worth 234 impressions) above one that fell
    18272->5497 (-70%, worth 12,775). Refresh burns an LLM call and edits a
    live page per candidate — spend them where the traffic is."""
    trivia = _article(url="https://x.ca/trivia", title="Trivia")
    real = _article(url="https://x.ca/real", title="Real traffic")
    _add_page(trivia, 235, W1_END)
    _add_page(trivia, 1, W2_END)      # -99.6%, but only 234 impressions
    _add_page(real, 18272, W1_END)
    _add_page(real, 5497, W2_END)     # -70%, but 12,775 impressions

    ranked = decaying_articles()
    assert [r["article_id"] for r in ranked] == [real, trivia]
    assert ranked[0]["impressions_lost"] == 12775


# ── report ──────────────────────────────────────────────────────


def _add_page_row(article_id, impressions, clicks, position, period_end, page=None):
    with get_session() as s:
        s.add(
            SearchPerformance(
                article_id=article_id,
                page=page or f"https://x.ca/{article_id or 'other'}",
                impressions=impressions, clicks=clicks, position=position,
                ctr=(clicks / impressions) if impressions else 0.0,
                period_start=period_end - timedelta(days=90), period_end=period_end,
            )
        )


def test_summary_is_unavailable_before_any_sync(gsc):
    assert site_summary()["available"] is False


def test_summary_totals_and_trend(gsc):
    a = _article()
    _add_page_row(a, 1000, 10, 10.0, W1_END)
    _add_page_row(a, 800, 8, 12.0, W2_END)

    s = site_summary()
    assert s["impressions"] == 800 and s["clicks"] == 8
    assert s["impressions_change_pct"] == -20.0
    assert s["clicks_change_pct"] == -20.0


def test_ctr_is_recomputed_from_totals_not_averaged(gsc):
    """Averaging each page's CTR would let a 2-impression page count as much
    as a 2,000-impression one and badly misreport the site."""
    a = _article(url="https://x.ca/a", title="A")
    b = _article(url="https://x.ca/b", title="B")
    _add_page_row(a, 2, 1, 5.0, W2_END)        # 50% CTR, negligible traffic
    _add_page_row(b, 1998, 19, 20.0, W2_END)   # ~1% CTR, all the traffic

    s = site_summary()
    assert s["impressions"] == 2000 and s["clicks"] == 20
    assert s["ctr"] == 0.01  # not the ~25.5% a naive mean would give


def test_position_is_impression_weighted(gsc):
    """An unweighted mean is dominated by the long tail of pages nobody sees."""
    a = _article(url="https://x.ca/a", title="A")
    b = _article(url="https://x.ca/b", title="B")
    _add_page_row(a, 9900, 1, 10.0, W2_END)
    _add_page_row(b, 100, 1, 90.0, W2_END)

    # Naive mean would be 50.0; weighted is ~10.8.
    assert round(site_summary()["position"], 1) == 10.8


def test_summary_without_a_previous_window_reports_no_trend(gsc):
    a = _article()
    _add_page_row(a, 500, 5, 10.0, W2_END)

    s = site_summary()
    assert s["available"] is True
    assert "previous" not in s


def test_top_pages_include_non_article_urls(gsc):
    """Product and collection pages carry real traffic and belong in the
    report even though refresh can't touch them."""
    _add_page_row(None, 5000, 10, 5.0, W2_END, page="https://x.ca/collections/vinyl")
    rows = top_pages()
    assert rows[0]["page"].endswith("/collections/vinyl")
    assert rows[0]["is_article"] is False


# ── overlapping windows ─────────────────────────────────────────


def _add_page_window(article_id, impressions, start, end):
    with get_session() as s:
        s.add(
            SearchPerformance(
                article_id=article_id, page=f"https://x.ca/{article_id}",
                impressions=impressions, position=10.0, clicks=1,
                period_start=start, period_end=end,
            )
        )


def test_decay_ignores_an_overlapping_window(gsc):
    """The real incident: sync-performance ran Monday and again Thursday, so
    the DB held two 90-day windows 1 day apart sharing 89 days of data.
    Comparing those ranks a day of noise as decay — it picked a random article
    over one that had lost 12,775 impressions.
    """
    article_id = _article()
    # Genuine prior window.
    _add_page_window(article_id, 1000, date(2026, 1, 14), date(2026, 4, 14))
    # Current, and a near-duplicate of it from a sync a day earlier.
    _add_page_window(article_id, 400, date(2026, 4, 13), date(2026, 7, 12))
    _add_page_window(article_id, 400, date(2026, 4, 14), date(2026, 7, 13))

    rows = decaying_articles()

    # Must compare 07-13 against 04-14 (1000 -> 400), not against 07-12 (0).
    assert len(rows) == 1
    assert rows[0]["impressions_before"] == 1000
    assert rows[0]["impressions_lost"] == 600


def test_no_non_overlapping_window_means_no_trend(gsc):
    """Two overlapping snapshots and nothing else is not a trend. Reporting
    their difference would be worse than reporting nothing."""
    article_id = _article()
    _add_page_window(article_id, 500, date(2026, 4, 13), date(2026, 7, 12))
    _add_page_window(article_id, 400, date(2026, 4, 14), date(2026, 7, 13))

    assert decaying_articles() == []


def test_adjacent_windows_are_comparable(gsc):
    """Touching but not overlapping — one ends exactly where the other starts.
    This is what a single sync produces and it must count."""
    article_id = _article()
    _add_page_window(article_id, 1000, date(2026, 1, 14), date(2026, 4, 14))
    _add_page_window(article_id, 250, date(2026, 4, 14), date(2026, 7, 13))

    rows = decaying_articles()
    assert len(rows) == 1
    assert rows[0]["impressions_lost"] == 750
