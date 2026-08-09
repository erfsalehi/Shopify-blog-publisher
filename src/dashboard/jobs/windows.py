"""Day-window planning, shared by every date-series sync.

Search Console, GA4 and Google Ads all have the same two awkward properties:
they restate their most recent days, and they hold more history than anyone
wants to re-fetch nightly. So all three want the same plan — everything
missing, plus a trailing re-pull — and all three want it chunked so an
interrupted run resumes.

This lives in one module because three copies of "which days do I need" is
precisely the code that drifts: someone fixes an off-by-one in the Search
Console copy and the Ads copy quietly keeps losing a day at the boundary.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterator


def daterange(start: date, end: date) -> Iterator[date]:
    """Every day from start to end, both inclusive."""
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def plan_window(
    existing: set[date],
    *,
    backfill_days: int,
    recent_days: int,
    today: date,
) -> tuple[date, date, list[date]]:
    """Which days to fetch: everything missing, plus a trailing re-pull.

    The trailing window is not an optimisation. Every source here restates
    recent days — Search Console as its data settles, GA4 as late events
    arrive, Google Ads as attribution windows close — so a sync that only
    fetched days it had never seen would freeze each day's first, partial
    reading permanently.

    Returns (start, end, days_to_fetch).
    """
    end = today
    if existing:
        # Never let the start creep forward past what we already hold: a
        # shortened backfill setting must not orphan history that's still
        # being charted.
        start = min(min(existing), end - timedelta(days=backfill_days))
    else:
        start = end - timedelta(days=backfill_days)
    repull_from = end - timedelta(days=recent_days)
    wanted = [
        day for day in daterange(start, end)
        if day >= repull_from or day not in existing
    ]
    return start, end, wanted


def chunks(days: list[date], size: int) -> list[tuple[date, date]]:
    """Group sorted days into runs of consecutive dates, each at most `size`.

    Consecutive-only because a chunk is fetched as one date range: a chunk
    spanning a gap would re-request days already held, which is the waste the
    fetch ledger exists to prevent.
    """
    out: list[tuple[date, date]] = []
    run: list[date] = []
    for day in days:
        if run and (day - run[-1]).days == 1 and len(run) < size:
            run.append(day)
        else:
            if run:
                out.append((run[0], run[-1]))
            run = [day]
    if run:
        out.append((run[0], run[-1]))
    return out
