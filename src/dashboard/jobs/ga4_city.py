"""GA4 city breakdown → `ga4_city_daily`.

The only city-level data this business already owns, and until now it wasn't
being stored. Search Console has no city dimension — country is as granular
as it gets — so without this, "how are we doing in Langley" cannot be
answered from any other table, and Langley is where the showroom is.

Two dimensions, one report each, because GA4 charges nothing per report and
combining `city` with `eventName` in a single call returns a row per city per
event per day, which is a much larger response for the same information.

Sessions and conversions land on the **same row**. The question is always the
ratio — a city sending traffic and producing no calls is the finding, and
splitting the two would put the interesting number behind a join.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from blog_pipeline.tools.analytics import AnalyticsClient

from dashboard import store
from dashboard.db import get_session
from dashboard.jobs.ga4 import SETTLING_DAYS, settled_through
from dashboard.jobs.registry import JobResult, JobSpec, register
from dashboard.models import Ga4CityDaily

log = logging.getLogger(__name__)

#: A GA4 city row with a handful of sessions is mostly noise, and the long
#: tail of one-session cities is thousands of rows a day. Kept anyway when it
#: converted — a single call from a city is the opposite of noise.
_MIN_SESSIONS = 2

#: GA4 reports "(not set)" for traffic it can't geolocate. Stored rather than
#: dropped: it's usually a meaningful share, and silently discarding it would
#: make the city percentages add up to less than the site total with no
#: explanation on the page.
_UNKNOWN = "(not set)"


def _int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _parse_day(value: str | None) -> date | None:
    text = str(value or "").strip()
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


def _rows_of(report) -> list[tuple[list, list]]:
    return [(r.get("dimensions") or [], r.get("metrics") or []) for r in report]


def sync_ga4_city(
    client: AnalyticsClient | None = None, today: date | None = None
) -> JobResult:
    client = client or AnalyticsClient()
    if not client.enabled:
        return JobResult(
            skipped=True,
            skip_reason=(
                "GA4 isn't configured — set GA4_PROPERTY_ID and "
                "GSC_CREDENTIALS_JSON in .env."
            ),
        )

    today = today or date.today()
    end = settled_through(today)
    days = max(7, min(int(store.get(store.GA4_BACKFILL_DAYS) or 180), 400))
    start = end - timedelta(days=days - 1)
    events = [e for e in (store.get(store.GA4_EVENTS) or []) if e]

    detail: dict = {
        "window": f"{start.isoformat()}..{end.isoformat()}",
        "settled_through": end.isoformat(),
        "conversion_events": events,
        "api_calls": 0,
    }

    # ── Sessions per city per day ───────────────────────────────────
    sessions_report = client.run_report(
        dimensions=["date", "city", "region", "country"],
        metrics=["sessions", "totalUsers", "engagedSessions"],
        start_date=start,
        end_date=end,
    )
    detail["api_calls"] += 1

    # (day, city) -> row values, so the events report below can merge into it.
    merged: dict[tuple[date, str], dict] = {}
    for dims, mets in _rows_of(sessions_report):
        day = _parse_day(dims[0] if dims else None)
        if day is None or not (start <= day <= end):
            continue
        city = (dims[1] if len(dims) > 1 else "") or _UNKNOWN
        merged[(day, city)] = {
            "region": dims[2] if len(dims) > 2 else None,
            "country": dims[3] if len(dims) > 3 else None,
            "sessions": _int(mets[0] if len(mets) > 0 else 0),
            "users": _int(mets[1] if len(mets) > 1 else 0),
            "engaged_sessions": _int(mets[2] if len(mets) > 2 else 0),
            "conversions": 0,
        }

    # ── Conversion events per city per day ──────────────────────────
    if events:
        events_report = client.run_report(
            dimensions=["date", "city", "eventName"],
            metrics=["eventCount"],
            start_date=start,
            end_date=end,
        )
        detail["api_calls"] += 1
        wanted = set(events)
        for dims, mets in _rows_of(events_report):
            day = _parse_day(dims[0] if dims else None)
            city = (dims[1] if len(dims) > 1 else "") or _UNKNOWN
            name = dims[2] if len(dims) > 2 else None
            if day is None or name not in wanted or not (start <= day <= end):
                continue
            row = merged.get((day, city))
            if row is None:
                # A conversion from a city with no session row. Rare, and
                # dropping it would lose the one event type that matters, so
                # the row is created with zero sessions rather than skipped.
                row = merged[(day, city)] = {
                    "region": None, "country": None, "sessions": 0,
                    "users": 0, "engaged_sessions": 0, "conversions": 0,
                }
            row["conversions"] += _int(mets[0] if mets else 0)

    now = datetime.now(timezone.utc)
    written = converting_cities = 0
    with get_session() as session:
        # Whole-window replace rather than upsert-per-row: GA4 restates late
        # events for ~48h, the window is bounded, and a delete+insert cannot
        # leave a half-updated day behind the way a partial upsert can.
        session.query(Ga4CityDaily).filter(
            Ga4CityDaily.date >= start, Ga4CityDaily.date <= end
        ).delete(synchronize_session=False)
        for (day, city), row in merged.items():
            if row["sessions"] < _MIN_SESSIONS and row["conversions"] == 0:
                continue
            session.add(Ga4CityDaily(
                date=day, city=city[:120],
                region=(row["region"] or None),
                country=(row["country"] or None),
                sessions=row["sessions"],
                users=row["users"],
                engaged_sessions=row["engaged_sessions"],
                conversions=row["conversions"],
                fetched_at=now,
            ))
            written += 1
            if row["conversions"]:
                converting_cities += 1

    detail["city_days"] = written
    detail["city_days_with_a_conversion"] = converting_cities
    detail["distinct_cities"] = len({c for _, c in merged})
    return JobResult(rows=written, detail=detail)


register(
    JobSpec(
        name="ga4_city",
        title="Analytics by city",
        description=(
            "Sessions and phone/WhatsApp conversions broken down by city. "
            "The only city-level data already owned — Search Console has no "
            "city dimension — and the basis of the Local SEO tab."
        ),
        fn=sync_ga4_city,
        enabled_key="jobs.ga4_city.enabled",
        hour_key="jobs.ga4_city.hour",
    )
)
