"""Preview a refresh, then publish exactly what was previewed.

This is the only code in the dashboard that changes a public page, so it is
worth being explicit about what it does and does not reimplement.

**It does not reimplement the refresh.** Generating a proposal is
`run_refresh(only_ids={id}, dry_run=True)` — the pipeline's own graph, its own
prompts, its own asset guards, unchanged.

**It does own the write**, because approval has to mean something. Re-running
the refresh on approval would call the model a second time and publish
something the owner never read; the change summary would still say
"shortened the intro" and the article would be different. So the proposal's
HTML is stored and that stored HTML is what gets written.

Owning the write means owning the guards that go with it, and all three are
re-checked here at apply time rather than trusted from preview time:

  1. **The live body must be unchanged** since the preview. If someone edited
     the post in Shopify admin, or the Wednesday cron refreshed it, applying
     an older proposal would silently revert them.
  2. **No dropped assets.** Re-run against the live body using the pipeline's
     own `lost_assets`, because a proposal generated an hour ago was checked
     against an hour-old body.
  3. **No leaked `[IMAGE - ...]` placeholder.**

And the snapshot is written before the overwrite, in its own committed
transaction, exactly as the pipeline does — it is the only undo Shopify offers
for a published post.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from blog_pipeline.db import Article, ArticleRevision
from blog_pipeline.db.models import RevisionReason
from blog_pipeline.db.session import get_session as pipeline_session
from blog_pipeline.graphs.refresh_graph import (
    has_stray_image_marker,
    lost_assets,
    run_refresh,
)
from blog_pipeline.tools.shopify import ShopifyClient, ShopifyError

from dashboard.db import get_session
from dashboard.models import RefreshProposal

log = logging.getLogger(__name__)


class RefreshRefused(RuntimeError):
    """Apply was stopped by a guard. The message is meant for the owner."""


def body_fingerprint(html: str | None) -> str:
    return hashlib.sha256((html or "").encode("utf-8")).hexdigest()


def preview(article_id: int) -> RefreshProposal:
    """Run a dry refresh for one article and store the proposal.

    Writes nothing to Shopify — `dry_run=True` is passed explicitly rather
    than relied on as a default, because this is the one place where getting
    it wrong publishes to a live site.
    """
    result = run_refresh(only_ids={article_id}, dry_run=True, limit=1)
    entries = result.get("articles") or []
    entry = next(
        (e for e in entries if e.get("article_id") == article_id),
        entries[0] if entries else None,
    )

    now = datetime.now(timezone.utc)
    if entry is None:
        proposal = RefreshProposal(
            article_id=article_id, status="failed", created_at=now,
            error=(
                "The refresh produced no result for this article. It may not "
                "be published to Shopify — only live posts can be refreshed."
            ),
        )
    elif entry.get("outcome") == "failed":
        proposal = RefreshProposal(
            article_id=article_id, status="failed", created_at=now,
            error=str(entry.get("error") or "Unknown failure."),
        )
    elif entry.get("outcome") == "skipped":
        proposal = RefreshProposal(
            article_id=article_id, status="skipped", created_at=now,
            error=(
                "The refresh agent decided this article doesn't need "
                "rewriting. That is a real answer, not an error."
            ),
        )
    else:
        original = entry.get("original_html") or ""
        proposal = RefreshProposal(
            article_id=article_id,
            status="pending",
            created_at=now,
            original_html=original,
            proposed_html=entry.get("proposed_html") or "",
            original_sha=body_fingerprint(original),
            changes_json=json.dumps(entry.get("changes") or []),
            image_suggestions_json=json.dumps(entry.get("image_suggestions") or []),
            input_tokens=int(result.get("input_tokens") or 0),
            output_tokens=int(result.get("output_tokens") or 0),
        )

    with get_session() as session:
        # One live proposal per article: an older pending one is superseded,
        # not left around to be applied by mistake later.
        session.query(RefreshProposal).filter(
            RefreshProposal.article_id == article_id,
            RefreshProposal.status == "pending",
        ).update({"status": "stale"}, synchronize_session=False)
        session.add(proposal)
        session.flush()
        session.expunge(proposal)
    return proposal


def apply(proposal_id: int) -> dict:
    """Publish a stored proposal to Shopify. Raises RefreshRefused on a guard.

    Never called by a scheduler, and there is deliberately no code path that
    does: every apply is a person clicking approve on a diff they have read.
    """
    with get_session() as session:
        proposal = session.get(RefreshProposal, proposal_id)
        if proposal is None:
            raise RefreshRefused(f"Proposal {proposal_id} no longer exists.")
        session.expunge(proposal)

    if proposal.status != "pending":
        raise RefreshRefused(
            f"This proposal is '{proposal.status}', not pending. Generate a "
            "fresh preview before publishing."
        )
    if not (proposal.proposed_html or "").strip():
        raise RefreshRefused("The proposal has no body to publish.")

    with pipeline_session() as session:
        article = session.get(Article, proposal.article_id)
        if article is None or not article.shopify_article_id:
            raise RefreshRefused(
                "That article isn't linked to a live Shopify post any more."
            )
        shopify_article_id = article.shopify_article_id
        article_title = article.title or article.topic

    shopify = ShopifyClient()
    try:
        live = shopify.fetch_article(shopify_article_id)
        live_body = live.get("body") or ""

        # Guard 1 — has the live page moved since the preview?
        if body_fingerprint(live_body) != (proposal.original_sha or ""):
            raise RefreshRefused(
                "The live article has changed since this preview was "
                "generated — someone edited it in Shopify, or the weekly "
                "refresh already ran. Publishing now would silently revert "
                "that. Generate a fresh preview and read the new diff."
            )

        # Guards 2 and 3 — re-checked against the body being replaced, using
        # the pipeline's own definitions so the two paths cannot drift.
        lost = lost_assets(live_body, proposal.proposed_html)
        if lost:
            raise RefreshRefused(
                f"Refusing to publish: the proposal drops {len(lost)} asset(s) "
                f"the live post has — {', '.join(lost[:3])}"
                f"{'…' if len(lost) > 3 else ''}."
            )
        if has_stray_image_marker(proposal.proposed_html):
            raise RefreshRefused(
                "Refusing to publish: the proposal still contains a bracketed "
                "[IMAGE - ...] placeholder, which would appear on the page."
            )

        # Snapshot first, committed on its own. If the write below fails, the
        # undo has to already exist — it is the only one Shopify offers.
        with pipeline_session() as session:
            session.add(ArticleRevision(
                article_id=proposal.article_id,
                body_html=live_body,
                title=live.get("title"),
                reason=RevisionReason.pre_refresh,
            ))

        published = shopify.update_article(
            shopify_article_id,
            body_html=proposal.proposed_html,
            dry_run=False,
        )
    finally:
        shopify.close()

    now = datetime.now(timezone.utc)
    with pipeline_session() as session:
        row = session.get(Article, proposal.article_id)
        if row is not None:
            row.draft_html = proposal.proposed_html

    with get_session() as session:
        row = session.get(RefreshProposal, proposal_id)
        if row is not None:
            row.status = "applied"
            row.applied_at = now

    log.info("applied refresh proposal %s to article %s",
             proposal_id, proposal.article_id)
    return {
        "article_id": proposal.article_id,
        "title": article_title,
        "url": getattr(published, "url", None),
        "applied_at": now,
    }


def latest_proposal(article_id: int) -> RefreshProposal | None:
    with get_session() as session:
        row = (
            session.query(RefreshProposal)
            .filter(RefreshProposal.article_id == article_id)
            .order_by(RefreshProposal.created_at.desc(), RefreshProposal.id.desc())
            .first()
        )
        if row is not None:
            session.expunge(row)
    return row


def proposals_for(article_id: int, limit: int = 10) -> list[RefreshProposal]:
    with get_session() as session:
        rows = (
            session.query(RefreshProposal)
            .filter(RefreshProposal.article_id == article_id)
            .order_by(RefreshProposal.created_at.desc(), RefreshProposal.id.desc())
            .limit(limit)
            .all()
        )
        for row in rows:
            session.expunge(row)
    return rows


def revisions_for(article_id: int) -> list[dict]:
    """Refresh history from the pipeline's snapshot table."""
    with pipeline_session() as session:
        rows = (
            session.query(ArticleRevision)
            .filter(ArticleRevision.article_id == article_id)
            .order_by(ArticleRevision.created_at.desc())
            .all()
        )
        return [
            {
                "id": r.id,
                "created_at": r.created_at,
                "title": r.title,
                "reason": getattr(r.reason, "name", str(r.reason)),
                "length": len(r.body_html or ""),
            }
            for r in rows
        ]
