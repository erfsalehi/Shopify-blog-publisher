"""Search Console → `gsc_site_daily` / `gsc_page_daily`.

Daily granularity, which is the whole reason this exists alongside the
pipeline's own `sync-performance`. That one stores two 90-day windows so it
can rank decaying posts; it cannot draw a trend line or answer "what happened
last Tuesday", and widening it wouldn't help — a 90-day total has no days in
it. So this job asks Google for the `date` dimension and keeps one row per day
forever.

Two properties make the job survivable on a machine behind a flaky proxy:

  * **Idempotent.** Rows are keyed by day and replaced, never appended, so
    running it twice is the same as running it once.
  * **Resumable.** Per-URL rows are fetched a chunk of days at a time and
    committed per chunk, and the next run only fetches days it doesn't
    already have. A backfill killed halfway costs nothing but the half.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from blog_pipeline.tools.search_console import SearchConsoleClient

from dashboard import store
from dashboard.db import get_session
from dashboard.jobs.registry import JobResult, JobSpec, register
from dashboard.jobs.windows import chunks, daterange, plan_window
from dashboard.models import (
    GscFetchDay,
    GscPageDaily,
    GscQueryDaily,
    GscSiteDaily,
)

log = logging.getLogger(__name__)

# Search Console finalises a day's numbers over the following two to three
# days. Rows inside this window are real but provisional — stored, re-pulled
# on the next sync, and marked as unsettled in the UI rather than quietly
# presented as a decline.
SETTLING_DAYS = 3


def settled_through(today: date | None = None) -> date:
    """The most recent day whose Search Console numbers can be trusted."""
    return (today or date.today()) - timedelta(days=SETTLING_DAYS)


def _parse_day(value: str | None) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _fetched_days(session, kind: str) -> set[date]:
    return {
        d
        for (d,) in session.query(GscFetchDay.date)
        .filter(GscFetchDay.kind == kind)
        .all()
    }


def _mark_fetched(session, kind: str, start: date, end: date) -> None:
    session.query(GscFetchDay).filter(
        GscFetchDay.kind == kind,
        GscFetchDay.date >= start,
        GscFetchDay.date <= end,
    ).delete(synchronize_session=False)
    now = datetime.now(timezone.utc)
    for day in daterange(start, end):
        session.add(GscFetchDay(date=day, kind=kind, fetched_at=now))


def _upsert_site_rows(session, rows: list[dict], start: date, end: date) -> int:
    """Replace the site totals for [start, end] with what Google just said."""
    session.query(GscSiteDaily).filter(
        GscSiteDaily.date >= start, GscSiteDaily.date <= end
    ).delete(synchronize_session=False)
    written = 0
    now = datetime.now(timezone.utc)
    for row in rows:
        day = _parse_day((row.get("keys") or [None])[0])
        if day is None or not (start <= day <= end):
            continue
        session.add(
            GscSiteDaily(
                date=day,
                clicks=int(row.get("clicks", 0)),
                impressions=int(row.get("impressions", 0)),
                ctr=float(row.get("ctr", 0.0)),
                position=float(row.get("position", 0.0)),
                fetched_at=now,
            )
        )
        written += 1
    return written


def _upsert_page_rows(session, rows: list[dict], start: date, end: date) -> int:
    session.query(GscPageDaily).filter(
        GscPageDaily.date >= start, GscPageDaily.date <= end
    ).delete(synchronize_session=False)
    written = 0
    now = datetime.now(timezone.utc)
    seen: set[tuple[date, str]] = set()
    for row in rows:
        keys = row.get("keys") or []
        day = _parse_day(keys[0] if keys else None)
        page = keys[1] if len(keys) > 1 else None
        if day is None or not page or not (start <= day <= end):
            continue
        # Google shouldn't repeat a (date, page) pair, but the table has a
        # unique constraint and one duplicate would abort the whole chunk.
        if (day, page) in seen:
            continue
        seen.add((day, page))
        session.add(
            GscPageDaily(
                date=day,
                page=page,
                clicks=int(row.get("clicks", 0)),
                impressions=int(row.get("impressions", 0)),
                ctr=float(row.get("ctr", 0.0)),
                position=float(row.get("position", 0.0)),
                fetched_at=now,
            )
        )
        written += 1
    return written


def _upsert_query_rows(session, rows: list[dict], start: date, end: date) -> int:
    session.query(GscQueryDaily).filter(
        GscQueryDaily.date >= start, GscQueryDaily.date <= end
    ).delete(synchronize_session=False)
    written = 0
    now = datetime.now()
    seen: set[tuple[date, str]] = set()
    for row in rows:
        keys = row.get("keys") or []
        day = _parse_day(keys[0] if keys else None)
        query = keys[1] if len(keys) > 1 else None
        if day is None or not query or not (start <= day <= end):
            continue
        if (day, query) in seen:
            continue
        seen.add((day, query))
        session.add(
            GscQueryDaily(
                date=day,
                query=query,
                clicks=int(row.get("clicks", 0)),
                impressions=int(row.get("impressions", 0)),
                ctr=float(row.get("ctr", 0.0)),
                position=float(row.get("position", 0.0)),
                fetched_at=now,
            )
        )
        written += 1
    return written


def sync_gsc_daily(client: SearchConsoleClient | None = None,
                   today: date | None = None) -> JobResult:
    client = client or SearchConsoleClient()
    if not client.enabled:
        return JobResult(
            skipped=True,
            skip_reason=(
                "Search Console isn't configured — set GSC_CREDENTIALS_JSON "
                "and GSC_SITE_URL in .env, then run "
                "`blog-pipeline sync-performance --list-sites` to confirm the "
                "service account can actually see the property."
            ),
            detail={"property": client.site_url or None},
        )

    today = today or date.today()
    backfill_days = store.get(store.GSC_BACKFILL_DAYS)
    recent_days = store.get(store.GSC_RECENT_DAYS)
    chunk_days = store.get(store.GSC_PAGE_CHUNK_DAYS)
    row_limit = store.get(store.GSC_PAGE_ROW_LIMIT)

    with get_session() as session:
        site_have = _fetched_days(session, "site")
        page_have = _fetched_days(session, "page")
        query_have = _fetched_days(session, "query")

    start, end, site_wanted = plan_window(
        site_have, backfill_days=backfill_days, recent_days=recent_days, today=today
    )
    _, _, page_wanted = plan_window(
        page_have, backfill_days=backfill_days, recent_days=recent_days, today=today
    )
    # Queries get their own, shorter backfill. There are far more distinct
    # search terms than URLs, and striking-distance analysis reads a recent
    # window — a year of daily query rows would be mostly one-impression
    # long-tail noise bought at the cost of a much longer first run.
    _, _, query_wanted = plan_window(
        query_have,
        backfill_days=min(backfill_days, store.get(store.GSC_QUERY_BACKFILL_DAYS)),
        recent_days=recent_days,
        today=today,
    )

    detail: dict = {
        "property": client.site_url,
        "window": f"{start.isoformat()}..{end.isoformat()}",
        "settled_through": settled_through(today).isoformat(),
        "api_calls": 0,
        "chunks": 0,
        "truncated_chunks": [],
    }
    site_rows_written = page_rows_written = query_rows_written = 0

    # Site totals come back one row per day, so the whole window is a single
    # request even on a first-run backfill.
    if site_wanted:
        s_start, s_end = min(site_wanted), max(site_wanted)
        rows = client.query(dimensions=["date"], start_date=s_start, end_date=s_end)
        detail["api_calls"] += 1
        with get_session() as session:
            site_rows_written = _upsert_site_rows(session, rows, s_start, s_end)
            _mark_fetched(session, "site", s_start, s_end)

    # Per-URL rows are the big pull: chunked, and committed per chunk so an
    # interrupted run keeps what it already fetched.
    for c_start, c_end in chunks(page_wanted, chunk_days):
        rows = client.query(
            dimensions=["date", "page"],
            start_date=c_start,
            end_date=c_end,
            row_limit=row_limit,
        )
        detail["api_calls"] += 1
        detail["chunks"] += 1
        truncated = len(rows) >= row_limit
        if truncated:
            # We asked for everything up to the cap and got exactly the cap,
            # so there is almost certainly more. Say so rather than storing a
            # silently incomplete day.
            detail["truncated_chunks"].append(
                f"{c_start.isoformat()}..{c_end.isoformat()}"
            )
            log.warning(
                "GSC page chunk %s..%s hit the %d-row cap; lower the chunk size",
                c_start, c_end, row_limit,
            )
        with get_session() as session:
            page_rows_written += _upsert_page_rows(session, rows, c_start, c_end)
            if not truncated:
                # A truncated chunk is deliberately left unmarked: partial
                # data is worth showing, but not worth freezing in place.
                _mark_fetched(session, "page", c_start, c_end)

    # Per-query rows, same chunk-and-commit shape as pages.
    for c_start, c_end in chunks(query_wanted, chunk_days):
        rows = client.query(
            dimensions=["date", "query"],
            start_date=c_start,
            end_date=c_end,
            row_limit=row_limit,
        )
        detail["api_calls"] += 1
        detail["chunks"] += 1
        truncated = len(rows) >= row_limit
        if truncated:
            detail["truncated_chunks"].append(
                f"queries {c_start.isoformat()}..{c_end.isoformat()}"
            )
            log.warning(
                "GSC query chunk %s..%s hit the %d-row cap", c_start, c_end, row_limit
            )
        with get_session() as session:
            query_rows_written += _upsert_query_rows(session, rows, c_start, c_end)
            if not truncated:
                _mark_fetched(session, "query", c_start, c_end)

    detail["site_days"] = site_rows_written
    detail["page_rows"] = page_rows_written
    detail["query_rows"] = query_rows_written
    return JobResult(
        rows=site_rows_written + page_rows_written + query_rows_written,
        detail=detail,
    )


register(
    JobSpec(
        name="gsc_daily",
        title="Search Console daily sync",
        description=(
            "Pulls per-day site totals and per-URL rows from Google Search "
            "Console, plus the search terms the site is shown for. Backfills on "
            "an empty database, then re-pulls the last few days each run "
            "because Google restates them."
        ),
        fn=sync_gsc_daily,
        enabled_key=store.JOB_GSC_ENABLED,
        hour_key=store.JOB_GSC_HOUR,
    )
)
