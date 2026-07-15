"""Health/observability metrics for the `status` command and coverage alerts.

Reports the PRD's dashboard signals from the local DB: articles synced to
Linear, average SEO score, average cost/article, sync error rate, and — the
key health metric — weeks of calendar coverage remaining.
"""

from __future__ import annotations

from datetime import date

from blog_pipeline.calendar import coverage_weeks
from blog_pipeline.config import get_settings
from blog_pipeline.db import Article, CalendarEntry, ContentCalendar, get_session
from blog_pipeline.db.models import ArticleStatus, EntryStatus, TopicSource


def gather_metrics(today: date | None = None) -> dict:
    today = today or date.today()
    settings = get_settings()
    with get_session() as session:
        # These numbers answer "how is the pipeline doing", so the imported
        # back catalogue is excluded: those posts are live but the pipeline
        # never wrote them, and counting them would inflate
        # articles_published and dilute error_rate. Dedup wants them; this
        # does not.
        articles = (
            session.query(Article)
            .filter(Article.topic_source != TopicSource.imported)
            .all()
        )
        synced = [a for a in articles if a.status == ArticleStatus.synced]
        published = [a for a in articles if a.status == ArticleStatus.published]
        failed = [a for a in articles if a.status == ArticleStatus.failed]

        # Metrics span both terminal-success states (synced to Linear + auto-
        # published live to Shopify).
        done = synced + published
        seo_scores = [a.seo_score for a in done if a.seo_score is not None]
        costs = [a.cost_usd for a in done if a.cost_usd]

        total_terminal = len(done) + len(failed)
        error_rate = (len(failed) / total_terminal) if total_terminal else 0.0

        cal = session.query(ContentCalendar).first()
        cadence = cal.cadence if cal else settings.cadence
        future_dates = [
            e.scheduled_date
            for e in session.query(CalendarEntry)
            .filter(CalendarEntry.status == EntryStatus.queued)
            .all()
            if e.scheduled_date >= today
        ]
        cov = coverage_weeks(future_dates, cadence, today)

        return {
            "articles_synced": len(synced),
            "articles_published": len(published),
            "articles_failed": len(failed),
            "avg_seo_score": round(sum(seo_scores) / len(seo_scores), 1)
            if seo_scores else None,
            "avg_cost_usd": round(sum(costs) / len(costs), 4) if costs else None,
            "error_rate": round(error_rate, 3),
            "coverage_weeks": cov,
            "queued_entries": len(future_dates),
            "cadence": cadence,
        }


def check_coverage_and_alert(today: date | None = None) -> float:
    """Return coverage weeks; fire a Slack alert if below 1 week."""
    from blog_pipeline.notify import send_coverage_alert

    metrics = gather_metrics(today)
    cov = metrics["coverage_weeks"]
    if cov < 1.0:
        send_coverage_alert(cov)
    return cov
