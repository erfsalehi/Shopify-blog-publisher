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

import html as html_module
import re
from datetime import datetime

from blog_pipeline.agents.convert import (
    BLOCK_MARKERS,
    apply_conversion_blocks,
    blocks_present,
    strip_blocks,
)
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


def _same_markup(a: str, b: str) -> bool:
    """Whether two bodies are the same page, ignoring how Shopify stored it.

    Shopify does not hand back what it was given: it inserts newlines inside
    our markup and returns `&rarr;` re-escaped as `&amp;rarr;` (which still
    renders as the arrow, so this is serialization, not corruption). Comparing
    raw strings therefore says "changed" on every single run, and a --replace
    would rewrite all 63 posts forever, each one taking a fresh revision
    snapshot for an edit that changes nothing a reader sees.

    Unescaping twice is what makes `&amp;rarr;` and `&rarr;` converge. Used
    only to compare — never to build what gets written.
    """
    def norm(markup: str) -> str:
        text = html_module.unescape(html_module.unescape(markup))
        # Attribute delimiters too, not just entities. A product whose title
        # contains an inch mark ("Moulding Baseboard 1/2\" x 2") is written
        # with `&quot;` inside a double-quoted attribute and handed back with
        # a literal quote inside a single-quoted one. Same page, and without
        # this that one post is rewritten on every run forever.
        return re.sub(r"\s+", " ", re.sub(r">\s+<", "><", text)).replace("'", '"').strip()

    return norm(a) == norm(b)


def backfill_conversion_blocks(
    *, limit: int = 250, dry_run: bool = True, replace: bool = False
) -> dict:
    """Inject the mid-article conversion blocks into already-published posts.

    The back catalogue is where the impressions already are, so it is where
    the blocks are worth the most — but these are live pages, and
    `update_article` edits public content the moment it runs with no staged
    variant to review.

    Three rules keep that safe:

      * **Dry run by default.** The caller has to ask for the write.
      * **No snapshot, no edit.** A post without an `Article` row can't have
        an `ArticleRevision` written for it, which means `rollback-refresh`
        couldn't undo it. Those are skipped and counted, not edited — run
        `import-existing` first to bring them in.
      * **Idempotent.** A post that already carries a block is left alone, so
        re-running tops up the remainder instead of stacking duplicates.

    Snapshots use `RevisionReason.pre_refresh`, the same reason the refresh
    agent writes, so `rollback-refresh <id>` is the undo for this too rather
    than a second mechanism doing the same job.
    """
    client = ShopifyClient()
    changed = skipped_existing = skipped_no_row = skipped_no_break = failed = 0
    touched: list[dict] = []
    errors: list[dict] = []
    posts: list[dict] = []
    try:
        products = client.list_product_cards()
        collections = client.list_collection_cards()
        posts = client.list_published(limit=limit)

        with get_session() as session:
            rows = {
                a.shopify_article_id: a.id
                for a in session.query(Article)
                .filter(Article.shopify_article_id.isnot(None))
                .all()
            }

        for post in posts:
            gid = post.get("id")
            title = (post.get("title") or "").strip()
            if not gid:
                continue
            article_id = rows.get(gid)
            if article_id is None:
                skipped_no_row += 1
                continue
            try:
                live = client.fetch_article(gid)
            except Exception as e:
                # Not just ShopifyError: a read timeout or a 5xx surfaces as
                # an httpx error, and letting one propagate aborts the whole
                # run part-written — which is exactly what happened on a live
                # run of 63 posts. One bad post costs that post, not the rest.
                failed += 1
                errors.append({"title": title, "error": f"{type(e).__name__}: {e}"})
                continue
            body = live.get("body") or ""
            # `replace` throws away what a previous run injected and starts
            # from the original prose, which is how an improvement to
            # placement or matching reaches posts that already have blocks.
            source = strip_blocks(body) if replace else body
            # Per block, not per post: a post that got cards before the phone
            # number was configured still needs its banner, and re-running is
            # how it gets one.
            present = blocks_present(source)
            if len(present) == len(BLOCK_MARKERS):
                skipped_existing += 1
                continue

            new_body = apply_conversion_blocks(
                body_html=source,
                campaign=title or live.get("handle") or "post",
                keywords=[title],
                products=products,
                collections=collections,
                skip=present,
            )
            if new_body == source:
                # Nothing placed: the post has fewer than two <h2> sections,
                # so there is no break to put a block at without splitting a
                # paragraph. Left alone rather than appended to.
                skipped_no_break += 1
                continue
            if _same_markup(new_body, body):
                # A --replace run that rebuilt exactly what is already live.
                # Counted as done rather than changed, so the summary doesn't
                # claim work that produced no edit.
                skipped_existing += 1
                continue

            if not dry_run:
                with get_session() as session:
                    session.add(
                        ArticleRevision(
                            article_id=article_id,
                            body_html=body,
                            title=live.get("title"),
                            reason=RevisionReason.pre_refresh,
                        )
                    )
                try:
                    client.update_article(gid, body_html=new_body)
                except Exception as e:
                    failed += 1
                    errors.append({"title": title, "error": f"{type(e).__name__}: {e}"})
                    continue
                with get_session() as session:
                    row = session.get(Article, article_id)
                    if row:
                        row.draft_html = new_body
            changed += 1
            touched.append({"article_id": article_id, "title": title})
    finally:
        client.close()

    return {
        "fetched": len(posts),
        "changed": changed,
        "skipped_already_had_blocks": skipped_existing,
        "skipped_not_imported": skipped_no_row,
        "skipped_no_section_break": skipped_no_break,
        "failed": failed,
        "errors": errors,
        "touched": touched,
        "dry_run": dry_run,
        "replace": replace,
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
