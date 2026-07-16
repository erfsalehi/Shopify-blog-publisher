"""Content refresh run: pick stale live posts, refresh them, write them back.

Selection is oldest-first over articles that are live on Shopify. That is a
weak proxy for "needs work" — age is not decay — and it's what's available
until Search Console performance lands, at which point candidates should be
ranked by decayed impressions instead. Deliberately not a LangGraph: like the
calendar, this is a linear pass with no branching or checkpointing.

Every write here edits a public, indexed page. Two things guard that:
  * dry_run defaults to True everywhere up the stack, so applying is opt-in.
  * the live body is snapshotted to ArticleRevision before the overwrite,
    which is the only undo Shopify gives us for a published post.

A per-article failure never aborts the run: the snapshot is already durable,
so the next article proceeds and the failure is reported at the end.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from blog_pipeline.agents.geo import apply_geo
from blog_pipeline.agents.refresh import refresh_article
from blog_pipeline.config import get_settings
from blog_pipeline.db import Article, ArticleRevision, get_session
from blog_pipeline.db.models import ArticleStatus, RevisionReason
from blog_pipeline.llm import CostTracker
from blog_pipeline.schemas import FAQItem
from blog_pipeline.tools.linear import LinearClient, LinearError
from blog_pipeline.tools.shopify import ShopifyClient, ShopifyError

REFRESH_LABEL = "Blog"


_IMG_SRC = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
_A_HREF = re.compile(r'<a[^>]+href=["\']([^"\']+)["\']', re.I)


def lost_assets(original: str, refreshed: str) -> list[str]:
    """Image sources and link targets the refresh dropped.

    The prompt tells the model to preserve every <figure>/<img> and <a href>,
    but nothing made it true — and a refresh goes live unreviewed, so a
    silently dropped image is a broken public page and a dropped internal link
    is an SEO regression. Both are invisible until someone looks at the post.

    Compares URLs, not markup: rewording an anchor or reformatting a figure is
    fine, losing the destination is not. Only reports losses — apply_geo
    legitimately adds links afterwards.
    """
    before = set(_IMG_SRC.findall(original)) | set(_A_HREF.findall(original))
    after = set(_IMG_SRC.findall(refreshed)) | set(_A_HREF.findall(refreshed))
    return sorted(before - after)


def _business_context() -> str:
    s = get_settings()
    bits = [b for b in (s.business_name, s.business_description) if b]
    if s.local_seo and s.business_location:
        bits.append(f"Serves {s.business_location}.")
    return " ".join(bits)


def select_stale_articles(session, *, older_than_months: int, limit: int) -> list[Article]:
    """Live posts, oldest first, older than the cutoff.

    Requires shopify_article_id: there's nothing to write back to without one.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=30 * older_than_months)
    return (
        session.query(Article)
        .filter(
            Article.status == ArticleStatus.published,
            Article.shopify_article_id.isnot(None),
            Article.published_at.isnot(None),
            Article.published_at < cutoff,
        )
        .order_by(Article.published_at.asc())
        .limit(limit)
        .all()
    )


def select_decaying_articles(session, *, limit: int) -> list[Article]:
    """Live posts whose impressions actually fell, worst first.

    Strictly better than age when the data exists: a 2023 post that still
    ranks should be left alone, while one that's quietly halved should jump
    the queue. Returns [] when there aren't two Search Console windows to
    compare — the caller falls back to age rather than guessing.
    """
    from blog_pipeline.performance import decaying_articles

    decayed = decaying_articles(limit=limit)
    if not decayed:
        return []
    ids = [d["article_id"] for d in decayed]
    rows = {
        a.id: a
        for a in session.query(Article)
        .filter(
            Article.id.in_(ids),
            Article.shopify_article_id.isnot(None),
        )
        .all()
    }
    # Preserve decay order, which the IN query does not.
    return [rows[i] for i in ids if i in rows]


def _sync_refresh_to_linear(
    client: LinearClient | None, *, title: str, url: str | None,
    changes: list[str], dry_run: bool,
) -> str | None:
    """Record what the refresh did. Best-effort: the Shopify write has already
    happened by now, so a Linear outage must not make it look like it didn't."""
    if client is None:
        return None
    lines = [
        f"♻️ **Refreshed an existing post.** {'(dry run — nothing written)' if dry_run else 'The live article has been updated in place.'}",
    ]
    if url:
        lines.append(f"\n**Live URL:** {url}")
    lines.append("\n### What changed")
    lines.extend(f"- {c}" for c in changes or ["(no summary returned)"])
    lines.append(
        "\n### If this refresh is wrong\n"
        "The previous body is snapshotted in the database — "
        "`blog-pipeline rollback-refresh --article-id <id>` restores it."
    )
    try:
        result = client.create_issue(
            title=f"Refreshed: {title}",
            description="\n".join(lines),
            state=get_settings().linear_review_state,
            labels=[REFRESH_LABEL],
            dry_run=dry_run,
        )
        return result.id
    except LinearError:
        return None


def run_refresh(
    *,
    older_than_months: int = 12,
    limit: int = 5,
    dry_run: bool = True,
) -> dict:
    """Refresh up to `limit` stale live posts. dry_run=True reports what would
    change without touching Shopify."""
    settings = get_settings()
    cost = CostTracker()
    results: list[dict] = []

    with get_session() as session:
        # Measured decay when Search Console has two windows to compare;
        # oldest-first otherwise. Age is only ever a stand-in for "has stopped
        # working", and it's a poor one — a post can be old and still ranking.
        candidates = select_decaying_articles(session, limit=limit)
        strategy = "decay"
        if not candidates:
            strategy = "age"
            candidates = select_stale_articles(
                session, older_than_months=older_than_months, limit=limit
            )
        # Detach what we need now: the Shopify/LLM calls below are slow, and
        # holding rows across them would pin the transaction open for minutes.
        targets = [
            {
                "id": a.id,
                "title": a.title or a.topic,
                "shopify_article_id": a.shopify_article_id,
                "shopify_url": a.shopify_url,
                "published_at": a.published_at,
            }
            for a in candidates
        ]

    if not targets:
        return {"considered": 0, "refreshed": 0, "skipped": 0, "failed": 0,
                "cost_usd": 0.0, "dry_run": dry_run, "selected_by": strategy,
                "articles": []}

    linear = None
    if settings.has_linear:
        try:
            linear = LinearClient()
        except LinearError:
            linear = None

    shopify = ShopifyClient()
    refreshed = skipped = failed = 0
    try:
        for target in targets:
            entry = {"article_id": target["id"], "title": target["title"]}
            try:
                live = shopify.fetch_article(target["shopify_article_id"])
                body = live.get("body") or ""
                if not body.strip():
                    raise ShopifyError("Shopify returned an empty body.")

                result = refresh_article(
                    title=live.get("title") or target["title"],
                    body_html=body,
                    published_at=target["published_at"],
                    business_context=_business_context(),
                    cost=cost,
                )
                if result.skipped:
                    skipped += 1
                    results.append({**entry, "outcome": "skipped"})
                    continue

                # Refuse to publish a body that lost an image or a link. This
                # write goes live with no human in the loop, so a degraded page
                # would simply be the page from here on. Better to fail this
                # one article loudly and leave the good version up.
                lost = lost_assets(body, result.body_html)
                if lost:
                    raise ShopifyError(
                        "Refusing to publish: the refresh dropped "
                        f"{len(lost)} asset(s) the original had — {', '.join(lost[:3])}"
                        f"{'…' if len(lost) > 3 else ''}"
                    )

                # Render the citation levers into the body — the same treatment
                # new articles get. Old posts predate all of it, so they're
                # precisely the ones missing the takeaways box, pull-quote, FAQ
                # section and JSON-LD that answer engines quote from.
                body_out = apply_geo(
                    body_html=result.body_html,
                    title=result.seo_title or live.get("title") or target["title"],
                    description=result.meta_description or "",
                    takeaways=result.key_takeaways,
                    faq=[FAQItem.model_validate(f) for f in result.faq],
                    pull_quote=result.pull_quote,
                    url=target.get("shopify_url"),
                )

                if not dry_run:
                    # Snapshot BEFORE the overwrite, in its own committed
                    # transaction: if the Shopify write or anything after it
                    # fails, the undo must still exist.
                    with get_session() as session:
                        session.add(
                            ArticleRevision(
                                article_id=target["id"],
                                body_html=body,
                                title=live.get("title"),
                                reason=RevisionReason.pre_refresh,
                            )
                        )

                published = shopify.update_article(
                    target["shopify_article_id"],
                    body_html=body_out,
                    title=result.seo_title or None,
                    seo_title=result.seo_title or None,
                    seo_description=result.meta_description or None,
                    dry_run=dry_run,
                )

                if not dry_run:
                    with get_session() as session:
                        row = session.get(Article, target["id"])
                        if row:
                            row.draft_html = body_out
                            if result.seo_title:
                                row.title = result.seo_title
                                row.seo_title = result.seo_title
                            if result.meta_description:
                                row.seo_description = result.meta_description

                _sync_refresh_to_linear(
                    linear,
                    title=target["title"],
                    url=published.url,
                    changes=result.change_summary,
                    dry_run=dry_run,
                )
                refreshed += 1
                results.append(
                    {**entry, "outcome": "refreshed", "changes": result.change_summary}
                )
            except Exception as e:
                # One bad article must not abort the batch: its snapshot is
                # already committed, and the remaining candidates are
                # independent. Report at the end instead.
                failed += 1
                results.append({**entry, "outcome": "failed", "error": str(e)})
    finally:
        shopify.close()
        if linear is not None:
            linear.close()

    return {
        "considered": len(targets),
        "refreshed": refreshed,
        "skipped": skipped,
        "failed": failed,
        "cost_usd": round(cost.usd, 4),
        "dry_run": dry_run,
        "selected_by": strategy,
        "articles": results,
    }
