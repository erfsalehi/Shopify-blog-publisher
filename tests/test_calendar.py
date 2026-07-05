from datetime import date

from blog_pipeline.calendar import (
    coverage_weeks,
    parse_cadence,
    publish_dates,
    slots_per_week,
)


def test_parse_cadence_explicit_days():
    assert parse_cadence("3x/week: Mon/Wed/Fri") == [0, 2, 4]
    assert parse_cadence("2x/week: Tue/Thu") == [1, 3]


def test_parse_cadence_fallback_from_count():
    # No explicit days -> spread by count.
    assert parse_cadence("3x/week") == [0, 2, 4]
    assert parse_cadence("1x/week") == [0]


def test_slots_per_week():
    assert slots_per_week("3x/week: Mon/Wed/Fri") == 3


def test_publish_dates_hits_only_cadence_weekdays():
    start = date(2026, 7, 5)  # Sunday
    dates = publish_dates("3x/week: Mon/Wed/Fri", weeks=2, start=start, occupied=set())
    # All dates must be Mon/Wed/Fri and strictly after start.
    assert all(d.weekday() in (0, 2, 4) for d in dates)
    assert all(d > start for d in dates)
    # 2 weeks * 3/week = ~6 slots.
    assert len(dates) == 6


def test_publish_dates_skips_occupied():
    start = date(2026, 7, 5)
    first = publish_dates("3x/week: Mon/Wed/Fri", weeks=2, start=start, occupied=set())
    occupied = {first[0]}
    second = publish_dates("3x/week: Mon/Wed/Fri", weeks=2, start=start, occupied=occupied)
    assert first[0] not in second
    assert len(second) == len(first) - 1


def test_coverage_weeks():
    today = date(2026, 7, 5)
    # 6 future slots at 3/week = 2.0 weeks of coverage.
    future = publish_dates("3x/week: Mon/Wed/Fri", weeks=2, start=today, occupied=set())
    assert coverage_weeks(future, "3x/week: Mon/Wed/Fri", today) == 2.0
    # Past dates don't count.
    assert coverage_weeks([date(2026, 1, 1)], "3x/week: Mon/Wed/Fri", today) == 0.0
