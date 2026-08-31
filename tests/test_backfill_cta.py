import pytest

from blog_pipeline import backfill
from blog_pipeline.backfill import backfill_conversion_blocks
from blog_pipeline.config import get_settings
from blog_pipeline.db import Article, ArticleRevision, get_session, init_db
from blog_pipeline.db.models import ArticleStatus, RevisionReason, TopicSource

BODY = (
    "<p>Intro.</p>"
    "<h2>One</h2><p>" + "word " * 80 + "</p>"
    "<h2>Two</h2><p>" + "word " * 80 + "</p>"
    "<h2>Three</h2><p>" + "word " * 80 + "</p>"
    "<h2>Four</h2><p>" + "word " * 80 + "</p>"
)

PRODUCTS = [{
    "title": "Venice Vinyl Plank", "url": "https://x/products/venice",
    "image": "https://cdn/x.jpg", "price": "$3.49",
}]
COLLECTIONS = [{
    "title": "Vinyl Plank Flooring", "url": "https://x/collections/vinyl",
    "image": "https://cdn/c.jpg",
}]


class FakeClient:
    """Stands in for ShopifyClient — records updates instead of making them."""

    def __init__(self, posts, bodies, fail_on=()):
        self.posts, self.bodies, self.fail_on = posts, bodies, set(fail_on)
        self.updates: dict[str, str] = {}
        self.closed = False

    better = False

    def list_product_cards(self):
        if self.better:
            return [{**PRODUCTS[0], "title": "Newly Added Vinyl Plank Range"}]
        return list(PRODUCTS)

    def list_collection_cards(self):
        return list(COLLECTIONS)

    def list_published(self, limit=250):
        return self.posts[:limit]

    def fetch_article(self, gid):
        return {"id": gid, "title": f"Post {gid}", "handle": f"post-{gid}",
                "body": self.bodies[gid]}

    def update_article(self, gid, *, body_html, **kw):
        if gid in self.fail_on:
            raise backfill.ShopifyError("boom")
        self.updates[gid] = body_html
        return None

    def close(self):
        self.closed = True


@pytest.fixture
def store(monkeypatch):
    """A phone number, a catalogue, and a patched Shopify client."""
    monkeypatch.setenv("BUSINESS_PHONE", "(604) 555-0134")
    monkeypatch.setenv("BUSINESS_NAME", "D&R Flooring")
    get_settings.cache_clear()
    init_db()
    made: dict = {}

    def install(posts, bodies, fail_on=()):
        client = FakeClient(posts, bodies, fail_on)
        monkeypatch.setattr(backfill, "ShopifyClient", lambda *a, **k: client)
        made["client"] = client
        return client

    yield install
    get_settings.cache_clear()


def _imported(gid: str, title: str) -> int:
    with get_session() as s:
        row = Article(
            topic=title, title=title, topic_source=TopicSource.imported,
            status=ArticleStatus.published, shopify_article_id=gid,
        )
        s.add(row)
        s.flush()
        return row.id


def test_dry_run_reports_work_without_touching_shopify(store):
    _imported("gid://shopify/Article/1", "Vinyl Plank Flooring Guide")
    client = store([{"id": "gid://shopify/Article/1", "title": "Vinyl Plank Flooring Guide"}],
                   {"gid://shopify/Article/1": BODY})
    result = backfill_conversion_blocks(dry_run=True)
    assert result["changed"] == 1
    assert client.updates == {}
    assert result["dry_run"] is True
    assert client.closed


def test_apply_writes_the_blocks_and_snapshots_the_old_body(store):
    article_id = _imported("gid://shopify/Article/1", "Vinyl Plank Flooring Guide")
    client = store([{"id": "gid://shopify/Article/1", "title": "Vinyl Plank Flooring Guide"}],
                   {"gid://shopify/Article/1": BODY})
    result = backfill_conversion_blocks(dry_run=False)

    assert result["changed"] == 1
    written = client.updates["gid://shopify/Article/1"]
    assert "call-banner" in written and "collection-cards" in written

    with get_session() as s:
        snap = s.query(ArticleRevision).filter_by(article_id=article_id).one()
        # the snapshot is the body as it was, so rollback-refresh can undo this
        assert snap.body_html == BODY
        assert snap.reason == RevisionReason.pre_refresh
        assert s.get(Article, article_id).draft_html == written


def test_a_post_with_every_block_is_left_alone(store):
    _imported("gid://shopify/Article/1", "Vinyl Plank Flooring Guide")
    done = (BODY + '<div class="call-banner">x</div>'
            + '<div class="product-cards">x</div>'
            + '<div class="collection-cards">x</div>')
    client = store([{"id": "gid://shopify/Article/1", "title": "Vinyl Plank Flooring Guide"}],
                   {"gid://shopify/Article/1": done})
    result = backfill_conversion_blocks(dry_run=False)
    assert result["skipped_already_had_blocks"] == 1
    assert result["changed"] == 0
    assert client.updates == {}


def test_a_post_with_only_some_blocks_is_topped_up(store):
    """The banner can't be added until a phone number exists, so a post that
    got cards first must still be able to get one later."""
    _imported("gid://shopify/Article/1", "Vinyl Plank Flooring Guide")
    partial = BODY.replace(
        "<h2>Three</h2>", '<div class="product-cards">existing</div><h2>Three</h2>', 1
    )
    client = store([{"id": "gid://shopify/Article/1", "title": "Vinyl Plank Flooring Guide"}],
                   {"gid://shopify/Article/1": partial})
    result = backfill_conversion_blocks(dry_run=False)

    assert result["changed"] == 1
    written = client.updates["gid://shopify/Article/1"]
    assert written.count('class="product-cards"') == 1  # not duplicated
    assert "call-banner" in written and "collection-cards" in written


def test_rerunning_is_idempotent(store):
    _imported("gid://shopify/Article/1", "Vinyl Plank Flooring Guide")
    bodies = {"gid://shopify/Article/1": BODY}
    client = store([{"id": "gid://shopify/Article/1", "title": "Vinyl Plank Flooring Guide"}], bodies)
    backfill_conversion_blocks(dry_run=False)
    # second run sees what the first one wrote
    bodies["gid://shopify/Article/1"] = client.updates["gid://shopify/Article/1"]
    again = backfill_conversion_blocks(dry_run=False)
    assert again["changed"] == 0
    assert again["skipped_already_had_blocks"] == 1


def test_a_post_with_no_article_row_is_never_edited(store):
    """No Article row means no snapshot, which means no undo — so no edit."""
    client = store([{"id": "gid://shopify/Article/9", "title": "Orphan"}],
                   {"gid://shopify/Article/9": BODY})
    result = backfill_conversion_blocks(dry_run=False)
    assert result["skipped_not_imported"] == 1
    assert result["changed"] == 0
    assert client.updates == {}


def test_a_post_without_section_breaks_is_skipped(store):
    _imported("gid://shopify/Article/1", "Vinyl Plank Flooring Guide")
    client = store([{"id": "gid://shopify/Article/1", "title": "Vinyl Plank Flooring Guide"}],
                   {"gid://shopify/Article/1": "<p>One flat paragraph.</p>"})
    result = backfill_conversion_blocks(dry_run=False)
    assert result["skipped_no_section_break"] == 1
    assert client.updates == {}


def test_one_failure_does_not_stop_the_rest(store):
    _imported("gid://shopify/Article/1", "Vinyl Plank Flooring Guide")
    _imported("gid://shopify/Article/2", "Vinyl Plank Installation Tips")
    client = store(
        [{"id": "gid://shopify/Article/1", "title": "Vinyl Plank Flooring Guide"},
         {"id": "gid://shopify/Article/2", "title": "Vinyl Plank Installation Tips"}],
        {"gid://shopify/Article/1": BODY, "gid://shopify/Article/2": BODY},
        fail_on=["gid://shopify/Article/1"],
    )
    result = backfill_conversion_blocks(dry_run=False)
    assert result["failed"] == 1
    assert result["changed"] == 1
    assert "gid://shopify/Article/2" in client.updates
    assert len(result["errors"]) == 1


def test_replace_redoes_a_post_that_already_has_blocks(store):
    """Placement and matching keep improving; without --replace the first
    version injected is the version a post keeps forever."""
    _imported("gid://shopify/Article/1", "Vinyl Plank Flooring Guide")
    bodies = {"gid://shopify/Article/1": BODY}
    client = store([{"id": "gid://shopify/Article/1", "title": "Vinyl Plank Flooring Guide"}], bodies)
    backfill_conversion_blocks(dry_run=False)
    first = client.updates["gid://shopify/Article/1"]
    bodies["gid://shopify/Article/1"] = first

    # Same inputs rebuild the same body, so there is no edit to make.
    assert backfill_conversion_blocks(dry_run=False, replace=True)["changed"] == 0

    # A better-matching catalogue is what --replace exists for.
    client.better = True
    again = backfill_conversion_blocks(dry_run=False, replace=True)
    assert again["changed"] == 1
    second = client.updates["gid://shopify/Article/1"]
    # rebuilt from the original prose, not layered on top of the last run
    assert second.count('class="product-cards"') == 1
    assert second.count('class="collection-cards"') == 1
    assert "Newly Added Vinyl Plank Range" in second


def test_replace_does_not_lose_the_original_prose(store):
    _imported("gid://shopify/Article/1", "Vinyl Plank Flooring Guide")
    bodies = {"gid://shopify/Article/1": BODY}
    client = store([{"id": "gid://shopify/Article/1", "title": "Vinyl Plank Flooring Guide"}], bodies)
    backfill_conversion_blocks(dry_run=False)
    bodies["gid://shopify/Article/1"] = client.updates["gid://shopify/Article/1"]
    backfill_conversion_blocks(dry_run=False, replace=True)

    from blog_pipeline.agents.convert import strip_blocks

    assert strip_blocks(client.updates["gid://shopify/Article/1"]) == BODY


def test_shopify_reformatting_is_not_mistaken_for_a_change(store):
    """Shopify returns bodies with newlines inserted and `&rarr;` re-escaped
    as `&amp;rarr;`. Compared raw, every --replace run would rewrite every
    post forever for an edit no reader would see."""
    from blog_pipeline.backfill import _same_markup

    ours = '<div class="c"><p>Shop the range &rarr;</p></div>'
    theirs = '<div class="c">\n<p>Shop the range &amp;rarr;</p>\n</div>'
    assert _same_markup(ours, theirs)
    assert not _same_markup(ours, '<div class="c"><p>Something else</p></div>')


def test_a_network_error_does_not_abort_the_run(store):
    """A read timeout is an httpx error, not a ShopifyError. Letting one
    propagate aborted a live 63-post run part-written."""
    _imported("gid://shopify/Article/1", "Vinyl Plank Flooring Guide")
    _imported("gid://shopify/Article/2", "Vinyl Plank Installation Tips")
    client = store(
        [{"id": "gid://shopify/Article/1", "title": "Vinyl Plank Flooring Guide"},
         {"id": "gid://shopify/Article/2", "title": "Vinyl Plank Installation Tips"}],
        {"gid://shopify/Article/1": BODY, "gid://shopify/Article/2": BODY},
    )

    real = client.update_article

    def flaky(gid, *, body_html, **kw):
        if gid == "gid://shopify/Article/1":
            raise TimeoutError("read timed out")
        return real(gid, body_html=body_html, **kw)

    client.update_article = flaky
    result = backfill_conversion_blocks(dry_run=False)

    assert result["failed"] == 1
    assert result["changed"] == 1
    assert "gid://shopify/Article/2" in client.updates
    assert "TimeoutError" in result["errors"][0]["error"]


def test_a_quote_in_a_product_title_does_not_churn():
    """Shopify rewrites `alt="a &quot;b&quot;"` as `alt='a "b"'` — same page,
    different bytes. One baseboard product with an inch mark in its title was
    enough to rewrite that post on every run forever."""
    from blog_pipeline.backfill import _same_markup

    ours = '<img alt="Baseboard 1/2&quot; x 2" src="x">'
    theirs = "<img alt='Baseboard 1/2\" x 2' src='x'>"
    assert _same_markup(ours, theirs)
    assert not _same_markup(ours, '<img alt="Baseboard 3/4 x 2" src="x">')
