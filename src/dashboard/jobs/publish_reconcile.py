"""Shopify's answer to "is this live?", written back to the pipeline.

The one job here that writes to the pipeline's `article` table, and the
docstring in `blog_articles.py` explains why that's normally forbidden: two
writers with no lock between them. This is the exception, and it earns it by
being narrow — it only ever copies a fact from the system that owns that
fact, and only in the direction that can't be wrong.

**Why it exists.** Five articles sat on the Blog page under "Draft in
Shopify — needs one click" for weeks. All five were already published and
publicly reachable; their Shopify `publishedAt` predated our own
`updated_at` by up to two weeks. Nothing was stuck. The pipeline records
`status` at the moment it writes a post and never asks again, so an article
published afterwards — by the pipeline's own publish step, or by a human in
Shopify admin — keeps saying `synced` forever.

That is not merely a cosmetic lie. `status` is what the refresh agent uses
to decide what's live and worth rewriting, and what dedup uses to know what
has already been said. A live article recorded as unpublished is invisible
to both.

**Only promotions.** If Shopify says published and we say otherwise, we were
wrong and Shopify is the authority. The reverse — we say published, Shopify
says not — is reported and *not* acted on: that direction is how an
un-publish, a deletion, or a bad API response would silently rewrite our own
history, and it needs a human to look at it rather than a nightly job.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from blog_pipeline.db import Article, ArticleStatus
from blog_pipeline.db.session import get_session as pipeline_session
from blog_pipeline.tools.shopify import ShopifyClient, ShopifyError

from dashboard.config import pipeline
from dashboard.jobs.registry import JobResult, JobSpec, register

log = logging.getLogger(__name__)

#: One article per query. Shopify's GraphQL cost budget makes a 60-article
#: batch a single expensive call that can throttle; this is a handful of
#: cheap ones, and the job is capped anyway.
_QUERY = "query($id: ID!){ article(id: $id){ id handle isPublished publishedAt } }"

#: Articles checked per run. Only ever the ones we believe are *not* live, so
#: this list shrinks to nothing as they reconcile and stays there.
_MAX_PER_RUN = 40


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def reconcile_publish_state() -> JobResult:
    settings = pipeline()
    if not settings.has_shopify:
        return JobResult(
            skipped=True,
            skip_reason=(
                "Shopify isn't configured — set SHOPIFY_STORE_DOMAIN and "
                "SHOPIFY_ACCESS_TOKEN."
            ),
        )

    with pipeline_session() as session:
        pending = (
            session.query(Article)
            .filter(
                Article.shopify_article_id.isnot(None),
                Article.status != ArticleStatus.published,
            )
            .order_by(Article.id)
            .limit(_MAX_PER_RUN)
            .all()
        )
        candidates = [
            (a.id, a.shopify_article_id, a.title or a.topic) for a in pending
        ]

    if not candidates:
        return JobResult(
            rows=0,
            detail={"note": "every article with a Shopify id is already "
                            "recorded as published"},
        )

    client = ShopifyClient()
    promoted: list[str] = []
    missing: list[str] = []
    still_draft = 0
    try:
        for article_id, gid, title in candidates:
            try:
                node = client.graphql(_QUERY, {"id": gid}).get("article")
            except ShopifyError as e:
                log.info("article %s lookup failed: %s", article_id, e)
                continue
            if node is None:
                # The id points at nothing. Deleted in admin, most likely.
                # Recorded, never acted on: clearing our own reference on the
                # strength of one API response is how history gets lost.
                missing.append(f"#{article_id} {title}")
                continue
            if not node.get("isPublished"):
                still_draft += 1
                continue

            with pipeline_session() as session:
                row = session.get(Article, article_id)
                if row is None or row.status == ArticleStatus.published:
                    continue
                row.status = ArticleStatus.published
                row.published_at = _parse(node.get("publishedAt")) or row.published_at
                if not row.shopify_url and node.get("handle"):
                    row.shopify_url = (
                        f"https://{client.domain}/blogs/news/{node['handle']}"
                    )
            promoted.append(f"#{article_id} {title}")
    finally:
        client.close()

    detail: dict = {
        "checked": len(candidates),
        "marked_published": len(promoted),
        "still_unpublished_in_shopify": still_draft,
    }
    if promoted:
        detail["promoted"] = promoted[:10]
    if missing:
        # Loud, because it means the Blog page is linking to something that
        # isn't there — and because we deliberately didn't touch it.
        detail["gone_from_shopify"] = missing[:10]

    return JobResult(rows=len(promoted), detail=detail)


register(
    JobSpec(
        name="publish_reconcile",
        title="Reconcile published state",
        description=(
            "Asks Shopify whether each article we think is unpublished is "
            "actually live, and records the answer. Articles published by "
            "hand in Shopify admin otherwise show as waiting forever — and "
            "stay invisible to the refresh agent, which only rewrites what "
            "it believes is live."
        ),
        fn=reconcile_publish_state,
        enabled_key="jobs.publish_reconcile.enabled",
        hour_key="jobs.publish_reconcile.hour",
    )
)
