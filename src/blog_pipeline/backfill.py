"""Pull the store's pre-existing Shopify posts into the Article table.

Both dedup checks — the calendar's topic filter and QA's duplicate check —
compare candidates only against rows in this table. A blog that predates the
pipeline is therefore invisible to them: research re-proposes posts the store
published years ago, and QA waves the near-duplicate through, because "no row
found" and "no duplicate exists" are the same answer to them.

Imported rows carry status=published (they are genuinely live) and
source=imported (they were never drafted here, so they have no seo_score or
cost — see metrics.py, which excludes them so the pipeline's own numbers stay
honest).

This is also the join key for what comes after: Search Console performance
attaches to an Article row, and the refresh agent draws its candidates from
them.
"""

from __future__ import annotations

from datetime import datetime

from blog_pipeline.config import get_settings
from blog_pipeline.db import Article, ArticleRevision, get_session
from blog_pipeline.db.models import ArticleStatus, RevisionReason, TopicSource
from blog_pipeline.tools.shopify import ShopifyClient, ShopifyError


def public_article_url(post: dict) -> str | None:
    """The canonical public URL for a Shopify post.

    Built from public_domain rather than the *.myshopify.com domain because
    this is the string Search Console reports pages under — get it wrong and
    performance data silently joins to nothing.
    """
    handle = (post.get("handle") or "").strip()
    blog_handle = ((post.get("blog") or {}).get("handle") or "").strip()
    base = get_settings().store_link_base
    if not (handle and blog_handle and base):
        return None
    return f"{base}/blogs/{blog_handle}/{handle}"


def parse_shopify_datetime(value: str | None) -> datetime | None:
    """Shopify hands back RFC3339 with a literal Z, which fromisoformat only
    learned to accept in 3.11+. Parse defensively — a post whose timestamp we
    can't read is still worth importing for dedup."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def import_shopify_articles(*, limit: int = 250, dry_run: bool = False) -> dict:
    """Upsert live Shopify posts into Article.

    Idempotent on shopify_article_id: re-running picks up only what's new and
    refreshes titles that changed in Shopify admin, so this is safe to put on
    a schedule.
    """
    client = ShopifyClient()
    try:
        posts = client.list_published(limit=limit)
    finally:
        client.close()

    created = updated = unchanged = 0
    with get_session() as session:
        existing = {
            a.shopify_article_id: a
            for a in session.query(Article)
            .filter(Article.shopify_article_id.isnot(None))
            .all()
        }
        for post in posts:
            gid = post.get("id")
            title = (post.get("title") or "").strip()
            if not gid or not title:
                continue
            published_at = parse_shopify_datetime(post.get("publishedAt"))
            row = existing.get(gid)
            if row is None:
                if not dry_run:
                    session.add(
                        Article(
                            topic=title,
                            title=title,
                            topic_source=TopicSource.imported,
                            status=ArticleStatus.published,
                            handle=post.get("handle"),
                            shopify_article_id=gid,
                            shopify_url=public_article_url(post),
                            published_at=published_at,
                        )
                    )
                created += 1
            elif row.title != title:
                # Retitled in Shopify admin since the last import; dedup keys
                # off the title, so a stale one silently stops matching.
                if not dry_run:
                    row.title = title
                    row.topic = title
                updated += 1
            else:
                unchanged += 1

    return {
        "fetched": len(posts),
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "dry_run": dry_run,
    }


def rollback_refresh(article_id: int, *, dry_run: bool = False) -> dict:
    """Restore an article's most recent pre-refresh snapshot to Shopify.

    The refresh agent edits live pages in place, so this is the undo. Restoring
    also records a `rollback` revision of what was live at the time — undoing a
    rollback has to be possible too, or this is just a differently-shaped way
    to lose content.
    """
    with get_session() as session:
        snapshot = (
            session.query(ArticleRevision)
            .filter(
                ArticleRevision.article_id == article_id,
                ArticleRevision.reason == RevisionReason.pre_refresh,
            )
            .order_by(ArticleRevision.created_at.desc())
            .first()
        )
        if snapshot is None:
            raise ValueError(
                f"No pre-refresh snapshot for article {article_id} — nothing to "
                "roll back to."
            )
        row = session.get(Article, article_id)
        if row is None or not row.shopify_article_id:
            raise ValueError(f"Article {article_id} has no Shopify post to restore.")
        body, title = snapshot.body_html or "", snapshot.title
        shopify_article_id = row.shopify_article_id
        taken_at = snapshot.created_at

    if not body.strip():
        raise ValueError(f"Snapshot for article {article_id} is empty; refusing.")

    client = ShopifyClient()
    try:
        current = client.fetch_article(shopify_article_id)
        result = client.update_article(
            shopify_article_id, body_html=body, title=title, dry_run=dry_run
        )
        if not dry_run:
            with get_session() as session:
                session.add(
                    ArticleRevision(
                        article_id=article_id,
                        body_html=current.get("body") or "",
                        title=current.get("title"),
                        reason=RevisionReason.rollback,
                    )
                )
                row = session.get(Article, article_id)
                if row:
                    row.draft_html = body
                    if title:
                        row.title = title
    except ShopifyError:
        raise
    finally:
        client.close()

    return {
        "article_id": article_id,
        "restored_from": taken_at.isoformat() if taken_at else None,
        "url": result.url,
        "dry_run": dry_run,
    }
