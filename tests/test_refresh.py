"""Refreshing live posts.

Every write in this path edits a public, indexed page — Shopify has no draft
revision for a published post. So the properties under test are mostly safety
ones: dry-run really is inert, the undo snapshot exists before the overwrite
rather than after, and one bad article doesn't abort the batch.
"""

from datetime import datetime, timedelta, timezone

import pytest

from blog_pipeline.backfill import rollback_refresh
from blog_pipeline.db import Article, ArticleRevision, get_session, init_db
from blog_pipeline.db.models import ArticleStatus, RevisionReason, TopicSource
from blog_pipeline.schemas import RefreshedArticle

OLD = datetime.now(timezone.utc) - timedelta(days=365 * 3)
RECENT = datetime.now(timezone.utc) - timedelta(days=10)


class _FakeShopify:
    def __init__(self, bodies=None, fail_on=None):
        self.bodies = bodies or {}
        self.fail_on = fail_on or set()
        self.updates = []
        self.closed = False

    def fetch_article(self, article_id):
        if article_id in self.fail_on:
            raise RuntimeError("shopify exploded")
        return {
            "id": article_id,
            "title": f"Live {article_id}",
            "body": self.bodies.get(article_id, "<p>original body</p>"),
            "handle": "h",
        }

    def update_article(self, article_id, *, body_html, dry_run=False, **kw):
        from blog_pipeline.tools.shopify import PublishResult

        if not dry_run:
            self.updates.append((article_id, body_html))
        return PublishResult(
            article_id=article_id, handle="h",
            url="https://drflooring.ca/blogs/news/h", dry_run=dry_run,
        )

    def close(self):
        self.closed = True


def _make_article(published_at=OLD, gid="gid://shopify/Article/1"):
    init_db()
    with get_session() as s:
        row = Article(
            topic="Old Post", title="Old Post",
            topic_source=TopicSource.imported, status=ArticleStatus.published,
            shopify_article_id=gid, published_at=published_at,
        )
        s.add(row)
        s.flush()
        return row.id


@pytest.fixture
def shopify(monkeypatch):
    fake = _FakeShopify()
    monkeypatch.setattr(
        "blog_pipeline.graphs.refresh_graph.ShopifyClient", lambda: fake
    )
    monkeypatch.setattr("blog_pipeline.backfill.ShopifyClient", lambda: fake)
    return fake


@pytest.fixture
def agent(monkeypatch):
    """Stub the LLM; return a setter for what it 'returns'."""
    holder = {
        "result": RefreshedArticle(
            body_html="<p>refreshed body</p>",
            change_summary=["Expanded the prep section"],
        )
    }
    monkeypatch.setattr(
        "blog_pipeline.graphs.refresh_graph.refresh_article",
        lambda **kw: holder["result"],
    )
    return holder


def _run(**kw):
    from blog_pipeline.graphs.refresh_graph import run_refresh

    return run_refresh(**kw)


def test_dry_run_does_not_touch_shopify(shopify, agent):
    _make_article()
    result = _run(dry_run=True)

    assert result["refreshed"] == 1
    assert shopify.updates == []  # the whole point
    with get_session() as s:
        assert s.query(ArticleRevision).count() == 0


def test_apply_writes_the_refreshed_body(shopify, agent):
    _make_article()
    result = _run(dry_run=False)

    assert result["refreshed"] == 1
    assert len(shopify.updates) == 1
    # Contains rather than equals: the body is passed through apply_geo on the
    # way out, which may wrap it with a takeaways box, pull-quote, FAQ section
    # and JSON-LD. The agent's prose must survive that intact.
    assert "<p>refreshed body</p>" in shopify.updates[0][1]


def test_the_citation_levers_are_rendered_into_the_written_body(shopify, agent, monkeypatch):
    """A refreshed post must get the same GEO treatment a new one does.

    Old posts predate that work entirely, so they're exactly the articles
    missing the FAQ block and pull-quote that answer engines quote — and the
    ones this pipeline most wants cited.
    """
    from blog_pipeline.schemas import RefreshedArticle

    monkeypatch.setattr(
        "blog_pipeline.graphs.refresh_graph.refresh_article",
        lambda **kw: RefreshedArticle(
            body_html="<p>refreshed body</p>",
            change_summary=["deepened it"],
            key_takeaways=["Linoleum is not waterproof."],
            faq=[{"question": "Is linoleum waterproof?", "answer": "No — it is water-resistant."}],
            pull_quote="Our installers seal every seam.",
        ),
    )
    _make_article()
    _run(dry_run=False)

    written = shopify.updates[0][1]
    assert "Is linoleum waterproof?" in written          # visible FAQ
    assert "Our installers seal every seam." in written  # pull-quote
    assert "Linoleum is not waterproof." in written      # takeaways box
    assert "application/ld+json" in written              # structured data


def test_the_previous_body_is_snapshotted_before_the_overwrite(shopify, agent):
    """The snapshot is the only undo — it must capture what was LIVE, not the
    refreshed version."""
    shopify.bodies["gid://shopify/Article/1"] = "<p>the original</p>"
    article_id = _make_article()
    _run(dry_run=False)

    with get_session() as s:
        snap = s.query(ArticleRevision).filter_by(article_id=article_id).one()
        assert snap.body_html == "<p>the original</p>"
        assert snap.reason is RevisionReason.pre_refresh


def test_a_skipped_article_is_left_completely_alone(shopify, agent):
    agent["result"] = RefreshedArticle(body_html="<p>x</p>", skipped=True)
    _make_article()
    result = _run(dry_run=False)

    assert result["skipped"] == 1
    assert result["refreshed"] == 0
    assert shopify.updates == []
    with get_session() as s:
        assert s.query(ArticleRevision).count() == 0  # nothing to undo


def test_recent_articles_are_not_candidates(shopify, agent):
    _make_article(published_at=RECENT)
    assert _run(older_than_months=12)["considered"] == 0


def test_one_failure_does_not_abort_the_batch(shopify, agent):
    """The snapshots are already durable; the rest of the batch is
    independent, so a single bad post must not strand it."""
    _make_article(gid="gid://shopify/Article/1")
    _make_article(gid="gid://shopify/Article/2")
    shopify.fail_on = {"gid://shopify/Article/1"}

    result = _run(dry_run=False)

    assert result["failed"] == 1
    assert result["refreshed"] == 1
    assert any(a["outcome"] == "failed" for a in result["articles"])


def test_an_article_with_no_shopify_id_is_never_selected():
    """Nothing to write back to — selecting it would only produce failures."""
    init_db()
    with get_session() as s:
        s.add(Article(topic="t", title="t", status=ArticleStatus.published,
                      shopify_article_id=None, published_at=OLD))

    from blog_pipeline.graphs.refresh_graph import select_stale_articles

    with get_session() as s:
        assert select_stale_articles(s, older_than_months=12, limit=5) == []


def test_falls_back_to_age_when_there_is_no_performance_data(shopify, agent):
    """Search Console is optional and needs two windows before it can rank
    anything. Refresh must still work meanwhile, not quietly select nothing."""
    _make_article()
    result = _run(dry_run=True)

    assert result["selected_by"] == "age"
    assert result["considered"] == 1


def test_decay_ranking_wins_when_performance_data_exists(shopify, agent, monkeypatch):
    """Age is a proxy for 'stopped working'; measured decay is the real
    thing, so it takes precedence when available."""
    from datetime import date, timedelta

    from blog_pipeline.db import SearchPerformance

    old_id = _make_article(published_at=OLD, gid="gid://shopify/Article/old")
    newer_id = _make_article(
        published_at=OLD + timedelta(days=200), gid="gid://shopify/Article/newer"
    )
    # The newer post is the one actually decaying — age ordering would miss it.
    with get_session() as s:
        for period_end, impressions in ((date(2026, 4, 1), 1000), (date(2026, 7, 1), 100)):
            s.add(
                SearchPerformance(
                    article_id=newer_id, page="https://x.ca/newer",
                    impressions=impressions, position=10.0, clicks=1,
                    period_start=period_end - timedelta(days=90),
                    period_end=period_end,
                )
            )
        for period_end, impressions in ((date(2026, 4, 1), 500), (date(2026, 7, 1), 900)):
            s.add(
                SearchPerformance(
                    article_id=old_id, page="https://x.ca/old",
                    impressions=impressions, position=10.0, clicks=1,
                    period_start=period_end - timedelta(days=90),
                    period_end=period_end,
                )
            )

    result = _run(dry_run=True, limit=5)

    assert result["selected_by"] == "decay"
    assert [a["article_id"] for a in result["articles"]] == [newer_id]


def test_oldest_first(shopify, agent):
    init_db()
    older = datetime.now(timezone.utc) - timedelta(days=365 * 5)
    _make_article(published_at=OLD, gid="gid://shopify/Article/newer")
    _make_article(published_at=older, gid="gid://shopify/Article/older")

    from blog_pipeline.graphs.refresh_graph import select_stale_articles

    with get_session() as s:
        picked = select_stale_articles(s, older_than_months=12, limit=5)
        assert picked[0].shopify_article_id == "gid://shopify/Article/older"


# ── rollback ────────────────────────────────────────────────────


def test_rollback_restores_the_snapshotted_body(shopify, agent):
    shopify.bodies["gid://shopify/Article/1"] = "<p>the original</p>"
    article_id = _make_article()
    _run(dry_run=False)
    shopify.bodies["gid://shopify/Article/1"] = "<p>refreshed body</p>"

    rollback_refresh(article_id, dry_run=False)

    assert shopify.updates[-1][1] == "<p>the original</p>"


def test_rollback_snapshots_what_it_replaced(shopify, agent):
    """Undoing a rollback has to be possible, or this is just another way to
    lose content."""
    article_id = _make_article()
    _run(dry_run=False)
    rollback_refresh(article_id, dry_run=False)

    with get_session() as s:
        reasons = [
            r.reason
            for r in s.query(ArticleRevision).filter_by(article_id=article_id).all()
        ]
    assert RevisionReason.rollback in reasons


def test_rollback_without_a_snapshot_refuses(shopify):
    article_id = _make_article()
    with pytest.raises(ValueError, match="No pre-refresh snapshot"):
        rollback_refresh(article_id)


def test_rollback_dry_run_does_not_write(shopify, agent):
    article_id = _make_article()
    _run(dry_run=False)
    before = len(shopify.updates)

    rollback_refresh(article_id, dry_run=True)

    assert len(shopify.updates) == before
