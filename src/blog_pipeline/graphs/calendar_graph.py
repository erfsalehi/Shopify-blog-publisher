"""Weekly Content Calendar agent.

Tops up a rolling topic queue to the coverage target: checks how many weeks are
already scheduled, and only if below target invokes topic research, dedupes the
candidates against synced articles + recent calendar entries, assigns publish
dates per the configured cadence, persists new CalendarEntry rows, mirrors each
one to a Linear issue (Backlog, due date = scheduled date) so the calendar is
visible there before drafting ever starts, and sends a Slack digest so a human
can reorder/veto before drafting begins. Idle weeks (queue already full) are a
no-op.

Implemented as a linear orchestration function — no interrupts or checkpointing
needed here, unlike the article graph.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from blog_pipeline.calendar import coverage_weeks, publish_dates, slots_per_week
from blog_pipeline.config import get_settings
from blog_pipeline.db import CalendarEntry, ContentCalendar, get_session
from blog_pipeline.db.models import EntryStatus, TopicSource
from blog_pipeline.dedup import filter_new_topics
from blog_pipeline.llm import CostTracker
from blog_pipeline.notify import send_calendar_digest
from blog_pipeline.schemas import TopicCandidate
from blog_pipeline.tools.linear import LinearClient, LinearError

CALENDAR_LABEL = "Blog"


def _sync_entry_to_linear(
    client: LinearClient | None, entry: CalendarEntry, cand: TopicCandidate
) -> None:
    """Create the Linear issue for a freshly-queued calendar entry.

    Best-effort: the entry still persists locally even if this fails, so a
    Linear outage never blocks the calendar refresh.
    """
    if client is None:
        return
    lines = [f"**Primary keyword:** {cand.primary_keyword}"]
    if cand.secondary_keywords:
        lines.append(f"**Secondary keywords:** {', '.join(cand.secondary_keywords)}")
    if cand.search_volume is not None:
        lines.append(f"**Search volume:** {cand.search_volume}")
    if cand.difficulty is not None:
        lines.append(f"**Difficulty:** {cand.difficulty}")
    lines.append(f"\n{cand.rationale}")
    try:
        result = client.create_issue(
            title=cand.topic,
            description="\n".join(lines),
            state="Backlog",
            due_date=entry.scheduled_date.isoformat(),
            labels=[CALENDAR_LABEL],
        )
        entry.linear_issue_id = result.id
        entry.linear_identifier = result.identifier
        entry.linear_url = result.url
    except LinearError:
        pass


def _get_or_create_calendar(session) -> ContentCalendar:
    settings = get_settings()
    cal = session.query(ContentCalendar).first()
    if cal is None:
        cal = ContentCalendar(
            cadence=settings.cadence,
            coverage_target_weeks=settings.coverage_target_weeks,
        )
        session.add(cal)
        session.flush()
    return cal


def _existing_topics(session, months_back: int = 6) -> list[str]:
    """Topics to dedupe against: recent calendar entries + synced articles."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=30 * months_back)
    entries = (
        session.query(CalendarEntry)
        .filter(CalendarEntry.created_at >= cutoff)
        .all()
    )
    topics = [e.topic for e in entries]
    from blog_pipeline.db.models import Article

    synced = session.query(Article).filter(Article.status == "synced").all()
    topics.extend(a.topic for a in synced)
    return topics


def run_calendar(
    *,
    niche: str | None = None,
    seed_keywords: list[str] | None = None,
    competitor_urls: list[str] | None = None,
    today: date | None = None,
    use_semantic: bool = True,
) -> dict:
    """Refresh the calendar. Returns a summary dict for the CLI/digest."""
    settings = get_settings()
    today = today or date.today()
    niche = niche or settings.niche
    seed_keywords = seed_keywords or settings.seed_keywords_list
    competitor_urls = competitor_urls or settings.competitor_urls_list

    cost = CostTracker()
    with get_session() as session:
        cal = _get_or_create_calendar(session)
        target_weeks = cal.coverage_target_weeks

        # Current coverage from future, non-published entries.
        future_dates = [
            e.scheduled_date
            for e in cal.entries
            if e.status in (EntryStatus.queued, EntryStatus.drafting, EntryStatus.drafted)
            and e.scheduled_date >= today
        ]
        current_cov = coverage_weeks(future_dates, cal.cadence, today)

        if current_cov >= target_weeks:
            cal.last_refreshed_at = datetime.now(timezone.utc)
            send_calendar_digest([], current_cov)
            return {
                "added": 0, "coverage_weeks": current_cov, "status": "full",
                "rejected": [],
            }

        # Open dates up to the target window, skipping occupied ones.
        occupied = {e.scheduled_date for e in cal.entries}
        open_dates = publish_dates(
            cal.cadence, weeks=target_weeks, start=today, occupied=occupied
        )
        needed = len(open_dates)
        if needed == 0:
            cal.last_refreshed_at = datetime.now(timezone.utc)
            return {"added": 0, "coverage_weeks": current_cov, "status": "no_slots",
                    "rejected": []}

        # Research more than needed to survive dedup attrition.
        if not settings.has_google:
            return {"added": 0, "coverage_weeks": current_cov,
                    "status": "no_llm_key", "rejected": []}

        from blog_pipeline.agents.topic_research import research_topics

        resolved_niche = niche or "general e-commerce"
        if not seed_keywords:
            from blog_pipeline.agents.seed_research import research_seed_keywords

            seed_keywords = research_seed_keywords(
                niche=resolved_niche, competitor_urls=competitor_urls, cost=cost
            )

        candidates: list[TopicCandidate] = research_topics(
            niche=resolved_niche,
            seed_keywords=seed_keywords,
            competitor_urls=competitor_urls,
            count=needed * 2,
            cost=cost,
        )

        existing = _existing_topics(session)
        cand_topics = [c.topic for c in candidates]
        kept_topics, rejected = filter_new_topics(
            cand_topics, existing, use_semantic=use_semantic
        )
        kept_set = set(kept_topics)
        kept_candidates = [c for c in candidates if c.topic in kept_set][:needed]

        added_summary: list[dict] = []
        linear_client = LinearClient() if settings.has_linear else None
        try:
            for cand, sched in zip(kept_candidates, open_dates):
                keywords = [cand.primary_keyword, *cand.secondary_keywords]
                entry = CalendarEntry(
                    calendar_id=cal.id,
                    scheduled_date=sched,
                    topic=cand.topic,
                    target_keywords=[k for k in keywords if k],
                    source=TopicSource.auto_researched,
                    status=EntryStatus.queued,
                    search_volume=cand.search_volume,
                    difficulty=cand.difficulty,
                    notes=cand.rationale,
                )
                session.add(entry)
                _sync_entry_to_linear(linear_client, entry, cand)
                added_summary.append(
                    {"scheduled_date": sched.isoformat(), "topic": cand.topic,
                     "primary_keyword": cand.primary_keyword,
                     "linear_identifier": entry.linear_identifier}
                )
        finally:
            if linear_client is not None:
                linear_client.close()

        cal.last_refreshed_at = datetime.now(timezone.utc)

        # New coverage after adding.
        new_future = future_dates + [d for _, d in zip(kept_candidates, open_dates)]
        new_cov = coverage_weeks(new_future, cal.cadence, today)

    send_calendar_digest(added_summary, new_cov)
    return {
        "added": len(added_summary),
        "coverage_weeks": new_cov,
        "status": "refreshed",
        "rejected": rejected,
        "cost_usd": cost.with_fee(),
        "seed_keywords": seed_keywords,
        "topics": added_summary,
    }


def get_due_entries(today: date | None = None) -> list[dict]:
    """Calendar entries scheduled for today that are still queued."""
    today = today or date.today()
    with get_session() as session:
        entries = (
            session.query(CalendarEntry)
            .filter(
                CalendarEntry.scheduled_date <= today,
                CalendarEntry.status == EntryStatus.queued,
            )
            .all()
        )
        return [
            {
                "id": e.id,
                "topic": e.topic,
                "target_keywords": e.target_keywords,
                "scheduled_date": e.scheduled_date.isoformat(),
            }
            for e in entries
        ]
