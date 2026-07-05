"""Cadence parsing, publish-date scheduling, and coverage math.

Pure functions (no DB/LLM) so the scheduling rules are unit-testable. The
calendar graph uses these to decide how many slots to fill and on which dates.

Cadence string format: "<n>x/week: Mon/Wed/Fri" — the weekday list is what
actually drives scheduling; the "3x" is descriptive. If no weekdays are given
we fall back to the count spread across the week.
"""

from __future__ import annotations

from datetime import date, timedelta

_WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}
_DEFAULT_SPREAD = {1: [0], 2: [0, 3], 3: [0, 2, 4], 4: [0, 2, 4, 6],
                   5: [0, 1, 2, 3, 4], 6: [0, 1, 2, 3, 4, 5], 7: [0, 1, 2, 3, 4, 5, 6]}


def parse_cadence(cadence: str) -> list[int]:
    """Return sorted weekday indexes (Mon=0) the cadence publishes on."""
    text = cadence.lower()
    days: list[int] = []
    if ":" in text:
        _, day_part = text.split(":", 1)
        for token in day_part.replace(",", "/").split("/"):
            key = token.strip()[:3]
            if key in _WEEKDAYS:
                days.append(_WEEKDAYS[key])
    if not days:
        # Fall back to "<n>x" spread across the week.
        n = 3
        for token in text.replace("/", " ").split():
            if token.endswith("x") and token[:-1].isdigit():
                n = int(token[:-1])
                break
        days = _DEFAULT_SPREAD.get(min(max(n, 1), 7), [0, 2, 4])
    return sorted(set(days))


def publish_dates(
    cadence: str, *, weeks: int, start: date, occupied: set[date] | None = None
) -> list[date]:
    """Generate publish dates over `weeks`, skipping already-occupied dates.

    Dates strictly after `start` are considered (today is not back-filled).
    """
    weekdays = parse_cadence(cadence)
    occupied = occupied or set()
    out: list[date] = []
    day = start + timedelta(days=1)
    horizon = start + timedelta(weeks=weeks)
    while day <= horizon:
        if day.weekday() in weekdays and day not in occupied:
            out.append(day)
        day += timedelta(days=1)
    return out


def slots_per_week(cadence: str) -> int:
    return len(parse_cadence(cadence))


def coverage_weeks(future_entry_dates: list[date], cadence: str, today: date) -> float:
    """Weeks of scheduled content remaining = future slots / slots-per-week."""
    per_week = slots_per_week(cadence) or 1
    future = [d for d in future_entry_dates if d >= today]
    return round(len(future) / per_week, 2)
