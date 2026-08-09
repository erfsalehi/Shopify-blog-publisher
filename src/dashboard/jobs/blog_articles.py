"""Pipeline database → `blog_article`.

The only job here that reads a database instead of an API. It copies article
metadata out of `blog_pipeline`'s own store so the Blog page can join articles
against this database's daily Search Console rows in one place.

Read-only, always. The pipeline is the only thing that writes an article, and
a dashboard that started editing that table would be two writers with no lock
between them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func

from blog_pipeline.db import Article, ArticleRevision
from blog_pipeline.db.session import get_session as pipeline_session

from dashboard.db import get_session
from dashboard.jobs.registry import JobResult, JobSpec, register
from dashboard.models import BlogArticle

log = logging.getLogger(__name__)


def _text(value) -> str | None:
    """Enum members stringify as 'ArticleStatus.published'; keep the label."""
    if value is None:
        return None
    return getattr(value, "name", None) or str(value)


def sync_blog_articles() -> JobResult:
    now = datetime.now(timezone.utc)

    try:
        with pipeline_session() as session:
            articles = session.query(Article).all()
            revisions = dict(
                session.query(
                    ArticleRevision.article_id,
                    func.count(ArticleRevision.id),
                ).group_by(ArticleRevision.article_id).all()
            )
            latest = dict(
                session.query(
                    ArticleRevision.article_id,
                    func.max(ArticleRevision.created_at),
                ).group_by(ArticleRevision.article_id).all()
            )
            rows = [
                {
                    "pipeline_id": a.id,
                    "title": a.title or a.topic or f"Article {a.id}",
                    "topic": a.topic,
                    "status": _text(a.status),
                    "shopify_url": a.shopify_url,
                    "shopify_article_id": a.shopify_article_id,
                    "linear_url": a.linear_url,
                    "linear_identifier": a.linear_identifier,
                    "published_at": a.published_at,
                    "revision_count": int(revisions.get(a.id, 0)),
                    "last_refreshed_at": latest.get(a.id),
                    "seo_score": a.seo_score,
                    "qa_confidence": a.qa_confidence_score,
                    "cost_usd": a.cost_usd or 0.0,
                    "synced_at": now,
                }
                for a in articles
            ]
    except Exception as exc:  # noqa: BLE001
        # A missing or un-migrated pipeline database is a configuration
        # problem, not a sync failure, and saying so beats an OperationalError.
        return JobResult(
            skipped=True,
            skip_reason=(
                f"Couldn't read the pipeline database ({type(exc).__name__}: "
                f"{exc}). Run `blog-pipeline init-db` and check DATABASE_URL."
            ),
        )

    created = updated = 0
    with get_session() as session:
        existing = {
            b.pipeline_id: b for b in session.query(BlogArticle).all()
        }
        for row in rows:
            current = existing.get(row["pipeline_id"])
            if current is None:
                session.add(BlogArticle(**row))
                created += 1
            else:
                for key, value in row.items():
                    setattr(current, key, value)
                updated += 1

    published = sum(1 for r in rows if r["shopify_url"])
    refreshed = sum(1 for r in rows if r["revision_count"])
    total_cost = round(sum(r["cost_usd"] for r in rows), 4)
    detail = {
        "articles": len(rows),
        "new": created,
        "updated": updated,
        "live_on_shopify": published,
        "refreshed_at_least_once": refreshed,
        "total_cost_usd": total_cost,
    }
    if rows and total_cost == 0:
        # PLAN.md Phase 2 calls this an instrumentation bug. It isn't:
        # `llm.MODEL_RATES` is empty deliberately because AI Studio's free tier
        # is rate-limited rather than billed, so $0.00 is the correct dollar
        # cost. The real gap was that CostTracker measured input/output tokens
        # and then discarded them — run_refresh now returns them, and each
        # refresh proposal records them.
        detail["cost_note"] = (
            "$0.00 is correct, not a bug: the free Gemini tier is rate-limited "
            "rather than billed. Token counts are the number that matters, and "
            "they're recorded per refresh on each article's page."
        )
    return JobResult(rows=created + updated, detail=detail)


register(
    JobSpec(
        name="blog_articles",
        title="Blog article index",
        description=(
            "Copies article metadata and refresh history from the pipeline's "
            "database so blog performance can be joined to daily search data. "
            "Read-only: the pipeline stays the only writer."
        ),
        fn=sync_blog_articles,
        enabled_key="jobs.blog_articles.enabled",
        hour_key="jobs.blog_articles.hour",
        # Local database read: no proxy in the path, so a failure here is real.
        max_attempts=1,
    )
)
