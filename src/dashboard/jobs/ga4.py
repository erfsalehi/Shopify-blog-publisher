"""GA4 → `ga4_daily` / `ga4_event_daily` / `ga4_blog_event_daily`.

Three reports, and the second one is the important one — the third says
which page earned it.

Sessions tell you how many people arrived. On this store that is not the
outcome: ~94% of the catalogue hides its price behind "Call for price", so
nobody checks out — they phone. The GTM events `call_click`,
`call_for_price_click` and `whatsapp_click` are therefore the store's actual
conversions, and they are the only place a business result is observable at
all. Search Console cannot see them; Shopify cannot see them.

That is why the event list is a setting rather than a constant: when someone
adds a `sms_click` or renames a tag in GTM, the fix must not be a code change.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from blog_pipeline.tools.analytics import AnalyticsClient

from dashboard import store
from dashboard.db import get_session
from dashboard.jobs.registry import JobResult, JobSpec, register
from dashboard.jobs.windows import plan_window
from dashboard.models import Ga4BlogEventDaily, Ga4Daily, Ga4EventDaily, GscFetchDay

log = logging.getLogger(__name__)

# GA4 keeps adjusting a day for roughly 48 hours as late events arrive. Less
# forgiving than Search Console's window, but the same trap: the freshest days
# always read low.
SETTLING_DAYS = 2

# The fetch ledger is shared with the Search Console job; these are its `kind`
# values. Separate kinds because the two sources cover different day ranges.
_KIND_TOTALS = "ga4_totals"
_KIND_EVENTS = "ga4_events"
_KIND_BLOG_EVENTS = "ga4_blog_events"


def settled_through(today: date | None = None) -> date:
    return (today or date.today()) - timedelta(days=SETTLING_DAYS)


def _parse_day(value: str | None) -> date | None:
    """GA4 returns dates as 'YYYYMMDD', not ISO."""
    text = str(value or "").strip()
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


def _int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _fetched_days(session, kind: str) -> set[date]:
    return {
        d for (d,) in session.query(GscFetchDay.date)
        .filter(GscFetchDay.kind == kind).all()
    }


def _mark_fetched(session, kind: str, start: date, end: date) -> None:
    session.query(GscFetchDay).filter(
        GscFetchDay.kind == kind,
        GscFetchDay.date >= start,
        GscFetchDay.date <= end,
    ).delete(synchronize_session=False)
    now = datetime.now(timezone.utc)
    day = start
    while day <= end:
        session.add(GscFetchDay(date=day, kind=kind, fetched_at=now))
        day += timedelta(days=1)


def _rows_of(report: list[dict]) -> list[tuple[list[str], list[str]]]:
    """Unpack AnalyticsClient.run_report's rows into (dimensions, metrics).

    Note it has *already* flattened GA4's `{dimensionValues: [{value: ...}]}`
    shape down to plain lists — re-flattening it yields empty rows, a
    successful API call, and a silent zero. Which is exactly what happened the
    first time this ran; `test_ga4_rows_are_read_in_the_clients_own_shape`
    exists to keep it from happening again.
    """
    return [
        (list(row.get("dimensions") or []), list(row.get("metrics") or []))
        for row in report
    ]


def sync_ga4_daily(
    client: AnalyticsClient | None = None, today: date | None = None
) -> JobResult:
    client = client or AnalyticsClient()
    if not client.enabled:
        return JobResult(
            skipped=True,
            skip_reason=(
                "GA4 isn't configured — set GA4_PROPERTY_ID (the NUMERIC id from "
                "Admin → Property Settings, not the G-XXXXXXX measurement id) and "
                "make sure the service account is a Viewer under Property access "
                "management."
            ),
            detail={"property": client.property_id or None},
        )

    today = today or date.today()
    backfill = store.get(store.GA4_BACKFILL_DAYS)
    recent = store.get(store.GA4_RECENT_DAYS)
    events = store.get(store.GA4_EVENTS)

    with get_session() as session:
        totals_have = _fetched_days(session, _KIND_TOTALS)
        events_have = _fetched_days(session, _KIND_EVENTS)
        blog_have = _fetched_days(session, _KIND_BLOG_EVENTS)

    detail: dict = {
        "property": client.property_id,
        "settled_through": settled_through(today).isoformat(),
        "conversion_events": events,
        "api_calls": 0,
    }
    total_rows = event_rows = 0

    # ── Site totals ─────────────────────────────────────────────────
    _, _, wanted = plan_window(
        totals_have, backfill_days=backfill, recent_days=recent, today=today
    )
    if wanted:
        start, end = min(wanted), max(wanted)
        report = client.run_report(
            dimensions=["date"],
            metrics=["sessions", "totalUsers", "engagedSessions"],
            start_date=start,
            end_date=end,
        )
        detail["api_calls"] += 1
        now = datetime.now(timezone.utc)
        with get_session() as session:
            session.query(Ga4Daily).filter(
                Ga4Daily.date >= start, Ga4Daily.date <= end
            ).delete(synchronize_session=False)
            for dims, mets in _rows_of(report):
                day = _parse_day(dims[0] if dims else None)
                if day is None or not (start <= day <= end):
                    continue
                session.add(Ga4Daily(
                    date=day,
                    sessions=_int(mets[0] if len(mets) > 0 else 0),
                    users=_int(mets[1] if len(mets) > 1 else 0),
                    engaged_sessions=_int(mets[2] if len(mets) > 2 else 0),
                    fetched_at=now,
                ))
                total_rows += 1
            _mark_fetched(session, _KIND_TOTALS, start, end)
        detail["window"] = f"{start.isoformat()}..{end.isoformat()}"

    # ── Conversion events ───────────────────────────────────────────
    # Pulled with eventName as a dimension rather than one report per event:
    # one call covers every event, and adding an event to the setting then
    # costs nothing. Rows for events we don't care about are dropped here.
    _, _, wanted = plan_window(
        events_have, backfill_days=backfill, recent_days=recent, today=today
    )
    if wanted and events:
        start, end = min(wanted), max(wanted)
        report = client.run_report(
            dimensions=["date", "eventName"],
            metrics=["eventCount"],
            start_date=start,
            end_date=end,
        )
        detail["api_calls"] += 1
        wanted_events = set(events)
        seen_names: set[str] = set()
        now = datetime.now(timezone.utc)
        with get_session() as session:
            session.query(Ga4EventDaily).filter(
                Ga4EventDaily.date >= start, Ga4EventDaily.date <= end
            ).delete(synchronize_session=False)
            for dims, mets in _rows_of(report):
                day = _parse_day(dims[0] if dims else None)
                name = dims[1] if len(dims) > 1 else None
                if day is None or not name:
                    continue
                seen_names.add(name)
                if name not in wanted_events or not (start <= day <= end):
                    continue
                session.add(Ga4EventDaily(
                    date=day, event_name=name,
                    event_count=_int(mets[0] if mets else 0),
                    fetched_at=now,
                ))
                event_rows += 1
            _mark_fetched(session, _KIND_EVENTS, start, end)

        # A configured event that GA4 has never heard of is almost always a
        # typo or a GTM tag that was renamed, and it would otherwise present
        # as "conversions: 0" — indistinguishable from a bad week.
        missing = sorted(wanted_events - seen_names)
        if missing:
            detail["events_not_found_in_ga4"] = missing
            log.warning("GA4 reported no rows for configured events: %s", missing)

    # ── The same events, attributed to the post that earned them ───
    # Sessions can be attributed to a post by UTM, but a phone call cannot:
    # the tel: link leaves no web trail, and GTM's site-wide call_click tag
    # doesn't know it fired on an article. GA4 does know — it stamps every
    # event with the page — so the attribution is a dimension we weren't
    # asking for rather than a tag we need to add.
    #
    # Its own fetch bookkeeping, not the event report's: sharing that marker
    # meant every day synced before this report existed counted as done, so
    # the history it was added to measure could never be backfilled.
    #
    # Deliberately NOT merged into the event report either — these rows are a
    # subset of it, and summing both would double every blog conversion, the
    # same shape of bug as the duplicate GTM tag documented in
    # store.GA4_EVENTS.
    blog_rows = 0
    _, _, wanted_blog = plan_window(
        blog_have, backfill_days=backfill, recent_days=recent, today=today
    )
    if wanted_blog and events:
        start, end = min(wanted_blog), max(wanted_blog)
        blog_report = client.run_report(
            dimensions=["date", "eventName", "pagePath"],
            metrics=["eventCount"],
            start_date=start,
            end_date=end,
            # Filtered by the API, not by us. Three dimensions over a
            # 180-day backfill runs to far more rows than the request limit,
            # and GA4 truncates silently — which drops the handful of blog
            # conversions this table exists to record and reports zero.
            dimension_filter={
                "andGroup": {"expressions": [
                    {"filter": {
                        "fieldName": "pagePath",
                        "stringFilter": {"matchType": "CONTAINS", "value": "/blogs/"},
                    }},
                    {"filter": {
                        "fieldName": "eventName",
                        "inListFilter": {"values": list(events)},
                    }},
                ]}
            },
        )
        detail["api_calls"] += 1
        wanted_events = set(events)
        now = datetime.now(timezone.utc)
        with get_session() as session:
            session.query(Ga4BlogEventDaily).filter(
                Ga4BlogEventDaily.date >= start, Ga4BlogEventDaily.date <= end
            ).delete(synchronize_session=False)
            for dims, mets in _rows_of(blog_report):
                day = _parse_day(dims[0] if dims else None)
                name = dims[1] if len(dims) > 1 else None
                path = dims[2] if len(dims) > 2 else None
                if day is None or not name or not path:
                    continue
                if "/blogs/" not in path or name not in wanted_events:
                    continue
                if not (start <= day <= end):
                    continue
                session.add(Ga4BlogEventDaily(
                    date=day, event_name=name, page_path=path[:500],
                    event_count=_int(mets[0] if mets else 0),
                    fetched_at=now,
                ))
                blog_rows += 1
            _mark_fetched(session, _KIND_BLOG_EVENTS, start, end)
    detail["blog_event_rows"] = blog_rows

    detail["total_days"] = total_rows
    detail["event_rows"] = event_rows
    return JobResult(rows=total_rows + event_rows + blog_rows, detail=detail)


register(
    JobSpec(
        name="ga4_daily",
        title="Analytics daily sync",
        description=(
            "Pulls daily sessions and users, plus the phone/WhatsApp click "
            "events that are this store's real conversions — nothing is "
            "checked out on the site, so these are the bottom line."
        ),
        fn=sync_ga4_daily,
        enabled_key="jobs.ga4_daily.enabled",
        hour_key="jobs.ga4_daily.hour",
    )
)
