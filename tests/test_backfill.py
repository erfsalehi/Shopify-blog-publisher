"""Importing the store's pre-pipeline back catalogue.

Both dedup checks compare only against the Article table, so a blog that
predates the pipeline reads as "no duplicate exists" — the store had 70 live
posts the calendar agent would happily re-propose.
"""

import pytest

from blog_pipeline.backfill import (
    import_shopify_articles,
    parse_shopify_datetime,
    public_article_url,
)
from blog_pipeline.db import Article, get_session, init_db
from blog_pipeline.db.models import ArticleStatus, TopicSource


def _post(n, title=None, blog="news", published="2023-04-01T10:00:00Z"):
    return {
        "id": f"gid://shopify/Article/{n}",
        "title": title or f"Post {n}",
        "handle": f"post-{n}",
        "publishedAt": published,
        "blog": {"handle": blog},
    }


class _FakeShopify:
    """list_published is the only surface backfill touches."""

    def __init__(self, posts):
        self._posts = posts
        self.closed = False

    def list_published(self, limit=250):
        return self._posts[:limit]

    def close(self):
        self.closed = True


@pytest.fixture
def store(monkeypatch):
    """Point backfill at a fake store; return a setter for its posts."""
    init_db()
    holder = {"posts": []}
    monkeypatch.setattr(
        "blog_pipeline.backfill.ShopifyClient", lambda: _FakeShopify(holder["posts"])
    )
    monkeypatch.setenv("PUBLIC_DOMAIN", "drflooring.ca")
    import blog_pipeline.config as config

    config.get_settings.cache_clear()
    yield holder
    config.get_settings.cache_clear()


def _rows():
    with get_session() as s:
        return s.query(Article).all()


def test_imports_every_post_as_a_live_imported_article(store):
    store["posts"] = [_post(1), _post(2), _post(3)]
    result = import_shopify_articles()

    assert result == {
        "fetched": 3, "created": 3, "updated": 0, "unchanged": 0, "dry_run": False,
    }
    rows = _rows()
    assert len(rows) == 3
    for row in rows:
        assert row.status is ArticleStatus.published  # they are genuinely live
        assert row.topic_source is TopicSource.imported
        assert row.shopify_article_id.startswith("gid://shopify/Article/")
        assert row.published_at is not None


def test_rerunning_creates_nothing(store):
    """Safe to schedule: re-running must not duplicate the whole catalogue."""
    store["posts"] = [_post(1), _post(2)]
    import_shopify_articles()
    again = import_shopify_articles()

    assert again["created"] == 0
    assert again["unchanged"] == 2
    assert len(_rows()) == 2


def test_a_post_retitled_in_shopify_updates_the_row(store):
    """Dedup keys off the title, so a stale one silently stops matching."""
    store["posts"] = [_post(1, title="Old Title")]
    import_shopify_articles()

    store["posts"] = [_post(1, title="New Title")]
    result = import_shopify_articles()

    assert result["updated"] == 1
    assert result["created"] == 0
    row = _rows()[0]
    assert row.title == "New Title"
    assert row.topic == "New Title"  # dedup reads topic, not title


def test_dry_run_reports_without_writing(store):
    store["posts"] = [_post(1), _post(2)]
    result = import_shopify_articles(dry_run=True)

    assert result["created"] == 2
    assert result["dry_run"] is True
    assert _rows() == []


def test_a_post_without_a_title_is_skipped_not_imported_blank(store):
    store["posts"] = [_post(1), {"id": "gid://shopify/Article/9", "title": "  "}]
    result = import_shopify_articles()

    assert result["created"] == 1


def test_url_is_the_public_domain_not_the_myshopify_one(monkeypatch):
    """Search Console reports pages under the public domain; joining on the
    *.myshopify.com URL would match nothing."""
    monkeypatch.setenv("PUBLIC_DOMAIN", "drflooring.ca")
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "https://b98e90.myshopify.com")
    import blog_pipeline.config as config

    config.get_settings.cache_clear()
    try:
        url = public_article_url(_post(1, blog="news"))
        assert url == "https://drflooring.ca/blogs/news/post-1"
    finally:
        config.get_settings.cache_clear()


def test_url_is_none_when_the_blog_handle_is_missing(monkeypatch):
    monkeypatch.setenv("PUBLIC_DOMAIN", "drflooring.ca")
    import blog_pipeline.config as config

    config.get_settings.cache_clear()
    try:
        assert public_article_url({"handle": "x", "blog": {}}) is None
    finally:
        config.get_settings.cache_clear()


@pytest.mark.parametrize(
    "value,expected_year",
    [("2022-11-07T19:30:25Z", 2022), ("2023-04-01T10:00:00+00:00", 2023)],
)
def test_parses_shopify_timestamps(value, expected_year):
    assert parse_shopify_datetime(value).year == expected_year


@pytest.mark.parametrize("value", [None, "", "not-a-date", 12345])
def test_an_unreadable_timestamp_does_not_lose_the_post(value):
    """The post still matters for dedup even if we can't read its date."""
    assert parse_shopify_datetime(value) is None


def test_imported_posts_are_visible_to_calendar_dedup(store):
    """The whole point: research must stop re-proposing the back catalogue."""
    store["posts"] = [_post(1, title="Is Hardwood Flooring Suitable For Your Home?")]
    import_shopify_articles()

    from blog_pipeline.graphs.calendar_graph import _existing_topics

    with get_session() as s:
        assert "Is Hardwood Flooring Suitable For Your Home?" in _existing_topics(s)


def test_imported_posts_are_visible_to_qa_dedup(store):
    store["posts"] = [_post(1, title="Best Flooring For Your Home")]
    import_shopify_articles()

    with get_session() as s:
        titles = [
            r[0]
            for r in s.query(Article.title)
            .filter(
                Article.status.in_([ArticleStatus.synced, ArticleStatus.published]),
                Article.title.isnot(None),
            )
            .all()
        ]
    assert "Best Flooring For Your Home" in titles


def test_imported_posts_do_not_inflate_pipeline_metrics(store):
    """status=published, but the pipeline never wrote them — counting them
    would claim 70 published articles on a pipeline that has produced none."""
    store["posts"] = [_post(n) for n in range(1, 71)]
    import_shopify_articles()

    from blog_pipeline.metrics import gather_metrics

    assert gather_metrics()["articles_published"] == 0
