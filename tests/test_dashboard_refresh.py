"""Preview and apply — the only dashboard code that changes a public page.

Weighted almost entirely toward refusals. A refresh that publishes something
slightly wrong is a broken page nobody notices; the guards are the feature,
and each one here corresponds to a way the live article could be silently
damaged.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from dashboard import diffing, refresh
from dashboard.db import get_session
from dashboard.models import RefreshProposal

ORIGINAL = (
    "<h2>Choosing a floor</h2><p>Old intro text.</p>"
    '<p><img src="https://cdn.shopify.com/a.jpg" alt="a"></p>'
    '<p><a href="https://drflooring.ca/collections/vinyl">Vinyl</a></p>'
)
PROPOSED = (
    "<h2>Choosing a floor</h2><p>A much clearer intro.</p>"
    '<p><img src="https://cdn.shopify.com/a.jpg" alt="a"></p>'
    '<p><a href="https://drflooring.ca/collections/vinyl">Vinyl</a></p>'
)


class FakeShopify:
    """Stands in for ShopifyClient over the two calls apply() makes."""

    def __init__(self, body=ORIGINAL, title="Choosing a floor"):
        self.body = body
        self.title = title
        self.updates: list[dict] = []
        self.closed = False

    def fetch_article(self, article_id):
        return {"body": self.body, "title": self.title}

    def update_article(self, article_id, *, body_html, dry_run=False, **kwargs):
        self.updates.append({
            "article_id": article_id, "body_html": body_html, "dry_run": dry_run
        })

        class Published:
            url = "https://drflooring.ca/blogs/news/x"

        return Published()

    def close(self):
        self.closed = True


@pytest.fixture
def live_article(monkeypatch, dashboard_db):
    """A published article in the pipeline DB, plus a stubbed Shopify."""
    from blog_pipeline.db import Article
    from blog_pipeline.db.session import get_session as pipeline_session
    from blog_pipeline.db.session import init_db as pipeline_init

    pipeline_init()
    with pipeline_session() as session:
        article = Article(
            topic="Choosing a floor", title="Choosing a floor",
            shopify_article_id="gid://shopify/Article/1",
            shopify_url="https://drflooring.ca/blogs/news/x",
        )
        session.add(article)
        session.flush()
        article_id = article.id

    shopify = FakeShopify()
    monkeypatch.setattr(refresh, "ShopifyClient", lambda *a, **k: shopify)
    return article_id, shopify


def _proposal(article_id, **overrides) -> RefreshProposal:
    fields = {
        "article_id": article_id,
        "status": "pending",
        "original_html": ORIGINAL,
        "proposed_html": PROPOSED,
        "original_sha": refresh.body_fingerprint(ORIGINAL),
        "changes_json": json.dumps(["Rewrote the intro"]),
    }
    fields.update(overrides)
    with get_session() as session:
        row = RefreshProposal(**fields)
        session.add(row)
        session.flush()
        session.expunge(row)
    return row


# ── Apply guards ───────────────────────────────────────────────────


def test_apply_publishes_exactly_the_previewed_html(live_article):
    """The whole point of approval: what was read is what ships. Re-running
    the model on approve would publish something nobody reviewed."""
    article_id, shopify = live_article
    proposal = _proposal(article_id)
    refresh.apply(proposal.id)
    assert len(shopify.updates) == 1
    assert shopify.updates[0]["body_html"] == PROPOSED
    assert shopify.updates[0]["dry_run"] is False


def test_apply_refuses_when_the_live_article_changed_since_the_preview(
    live_article,
):
    """Someone edited it in Shopify admin, or the Wednesday cron already ran.
    Publishing an older proposal would silently revert them."""
    article_id, shopify = live_article
    proposal = _proposal(article_id)
    shopify.body = ORIGINAL + "<p>A human added this by hand.</p>"
    with pytest.raises(refresh.RefreshRefused, match="changed since"):
        refresh.apply(proposal.id)
    assert shopify.updates == []


def test_apply_refuses_a_proposal_that_drops_an_image(live_article):
    """A dropped figure is a broken public page nobody notices. Re-checked
    here against the live body, not trusted from preview time."""
    article_id, shopify = live_article
    stripped = PROPOSED.replace(
        '<p><img src="https://cdn.shopify.com/a.jpg" alt="a"></p>', ""
    )
    proposal = _proposal(article_id, proposed_html=stripped)
    with pytest.raises(refresh.RefreshRefused, match="drops"):
        refresh.apply(proposal.id)
    assert shopify.updates == []


def test_apply_refuses_a_proposal_that_drops_a_link(live_article):
    article_id, shopify = live_article
    stripped = PROPOSED.replace(
        '<p><a href="https://drflooring.ca/collections/vinyl">Vinyl</a></p>', ""
    )
    proposal = _proposal(article_id, proposed_html=stripped)
    with pytest.raises(refresh.RefreshRefused, match="drops"):
        refresh.apply(proposal.id)
    assert shopify.updates == []


def test_apply_refuses_a_leaked_image_placeholder(live_article):
    """`[IMAGE - ...]` markers are for drafts. On a live page it's literal
    bracket text where a photo should be."""
    article_id, shopify = live_article
    leaked = PROPOSED + "<p><strong>[IMAGE - hero: a wide plank floor]</strong></p>"
    proposal = _proposal(article_id, proposed_html=leaked)
    with pytest.raises(refresh.RefreshRefused, match="placeholder"):
        refresh.apply(proposal.id)
    assert shopify.updates == []


def test_apply_refuses_an_already_applied_proposal(live_article):
    article_id, shopify = live_article
    proposal = _proposal(article_id)
    refresh.apply(proposal.id)
    shopify.updates.clear()
    with pytest.raises(refresh.RefreshRefused, match="not pending"):
        refresh.apply(proposal.id)
    assert shopify.updates == []


def test_apply_refuses_an_empty_body(live_article):
    article_id, shopify = live_article
    proposal = _proposal(article_id, proposed_html="   ")
    with pytest.raises(refresh.RefreshRefused, match="no body"):
        refresh.apply(proposal.id)
    assert shopify.updates == []


def test_the_previous_body_is_snapshotted_before_the_write(live_article):
    """Shopify has no draft revision for a published post — this snapshot is
    the only undo, so it has to exist before the overwrite, not after."""
    from blog_pipeline.db import ArticleRevision
    from blog_pipeline.db.session import get_session as pipeline_session

    article_id, _ = live_article
    proposal = _proposal(article_id)
    refresh.apply(proposal.id)
    with pipeline_session() as session:
        revisions = session.query(ArticleRevision).filter(
            ArticleRevision.article_id == article_id
        ).all()
    assert len(revisions) == 1
    assert revisions[0].body_html == ORIGINAL


def test_a_refused_apply_leaves_no_snapshot_behind(live_article):
    """A snapshot with no corresponding write is a confusing history entry
    implying a refresh happened."""
    from blog_pipeline.db import ArticleRevision
    from blog_pipeline.db.session import get_session as pipeline_session

    article_id, shopify = live_article
    shopify.body = ORIGINAL + "<p>edited</p>"
    proposal = _proposal(article_id)
    with pytest.raises(refresh.RefreshRefused):
        refresh.apply(proposal.id)
    with pipeline_session() as session:
        assert session.query(ArticleRevision).count() == 0


def test_apply_marks_the_proposal_applied(live_article):
    article_id, _ = live_article
    proposal = _proposal(article_id)
    refresh.apply(proposal.id)
    with get_session() as session:
        row = session.get(RefreshProposal, proposal.id)
        assert row.status == "applied"
        assert row.applied_at is not None


# ── Preview ────────────────────────────────────────────────────────


def test_preview_supersedes_an_earlier_pending_proposal(dashboard_db, monkeypatch):
    """Two live proposals for one article means the wrong one can be applied
    later by mistake."""
    monkeypatch.setattr(refresh, "run_refresh", lambda **kw: {
        "articles": [{
            "article_id": 7, "outcome": "refreshed",
            "changes": ["x"], "image_suggestions": [],
            "original_html": ORIGINAL, "proposed_html": PROPOSED,
        }],
        "input_tokens": 100, "output_tokens": 50,
    })
    first = refresh.preview(7)
    second = refresh.preview(7)
    with get_session() as session:
        assert session.get(RefreshProposal, first.id).status == "stale"
        assert session.get(RefreshProposal, second.id).status == "pending"


def test_preview_always_passes_dry_run_true(dashboard_db, monkeypatch):
    """This is the one place where relying on a default would publish to a
    live site if the default ever moved."""
    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return {"articles": [], "input_tokens": 0, "output_tokens": 0}

    monkeypatch.setattr(refresh, "run_refresh", fake)
    refresh.preview(7)
    assert seen["dry_run"] is True
    assert seen["only_ids"] == {7}


def test_a_skipped_refresh_is_recorded_as_a_real_answer(dashboard_db, monkeypatch):
    """The agent deciding an article doesn't need rewriting is information,
    not a failure."""
    monkeypatch.setattr(refresh, "run_refresh", lambda **kw: {
        "articles": [{"article_id": 7, "outcome": "skipped"}],
        "input_tokens": 0, "output_tokens": 0,
    })
    proposal = refresh.preview(7)
    assert proposal.status == "skipped"
    assert "doesn't need rewriting" in proposal.error


def test_preview_records_token_usage(dashboard_db, monkeypatch):
    """Dollars are correctly $0 on the free tier; tokens are the resource
    actually constrained, and they were being thrown away."""
    monkeypatch.setattr(refresh, "run_refresh", lambda **kw: {
        "articles": [{
            "article_id": 7, "outcome": "refreshed", "changes": [],
            "image_suggestions": [],
            "original_html": ORIGINAL, "proposed_html": PROPOSED,
        }],
        "input_tokens": 4210, "output_tokens": 1880,
    })
    proposal = refresh.preview(7)
    assert proposal.input_tokens == 4210
    assert proposal.output_tokens == 1880


# ── Diffing ────────────────────────────────────────────────────────


def test_the_diff_shows_the_changed_paragraph_and_not_the_rest(dashboard_db):
    lines = diffing.diff(ORIGINAL, PROPOSED)
    added = [line.text for line in lines if line.kind == "added"]
    removed = [line.text for line in lines if line.kind == "removed"]
    assert added == ["A much clearer intro."]
    assert removed == ["Old intro text."]
    # The heading is untouched and must not read as a rewrite.
    assert any(line.kind == "same" and "Choosing a floor" in line.text
               for line in lines)


def test_reflowed_whitespace_is_not_a_change(dashboard_db):
    """A re-wrapped paragraph would otherwise light up the whole diff and
    bury the real edits."""
    spaced = ORIGINAL.replace("<p>Old intro text.</p>",
                              "<p>Old\n   intro     text.</p>")
    lines = diffing.diff(ORIGINAL, spaced)
    assert not any(line.kind in ("added", "removed") for line in lines)


def test_embedded_json_ld_is_ignored_by_the_diff(dashboard_db):
    """The GEO step re-emits JSON-LD on every refresh; a regenerated
    timestamp inside it would show as a content change every time."""
    with_schema = PROPOSED + '<script type="application/ld+json">{"a":1}</script>'
    other_schema = PROPOSED + '<script type="application/ld+json">{"a":2}</script>'
    lines = diffing.diff(with_schema, other_schema)
    assert not any(line.kind in ("added", "removed") for line in lines)


def test_collapse_keeps_context_and_marks_the_elision(dashboard_db):
    long_original = "".join(f"<p>Para {n}</p>" for n in range(30))
    long_proposed = long_original.replace("<p>Para 15</p>", "<p>Rewritten 15</p>")
    collapsed = diffing.collapse(diffing.diff(long_original, long_proposed))
    assert None in collapsed  # an elision is marked, not silently dropped
    kept = [line for line in collapsed if line is not None]
    assert len(kept) < 30
    assert any(line.text == "Rewritten 15" for line in kept)


def test_summarise_counts_words_both_sides(dashboard_db):
    summary = diffing.summarise(ORIGINAL, PROPOSED)
    assert summary["added"] == 1
    assert summary["removed"] == 1
    assert summary["words_before"] > 0
    assert summary["words_after"] > 0
