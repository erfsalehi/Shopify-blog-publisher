"""Read-side arithmetic.

Both of these are wrong-in-a-plausible-looking-way bugs rather than crashes,
which is why they get tests instead of comments: a dashboard that reports a
CTR nobody can reproduce in Google's own UI is worse than no dashboard.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from dashboard.db import get_session
from dashboard.models import GscPageDaily, GscSiteDaily
from dashboard.reporting import site_series, site_summary, top_pages

TODAY = date(2026, 8, 5)


def _add_days(rows):
    with get_session() as session:
        for day, clicks, impressions, position in rows:
            session.add(
                GscSiteDaily(
                    date=day,
                    clicks=clicks,
                    impressions=impressions,
                    ctr=(clicks / impressions if impressions else 0.0),
                    position=position,
                )
            )


def test_ctr_is_recomputed_from_totals_not_averaged(dashboard_db):
    """One tiny day at 100% CTR alongside a big day at 1% must not report 50%."""
    end = TODAY - timedelta(days=3)
    _add_days([
        (end, 1, 1, 5.0),                       # 100% CTR, one impression
        (end - timedelta(days=1), 10, 1000, 5.0),  # 1% CTR, a thousand
    ])
    summary = site_summary(window_days=7, today=TODAY)
    ctr = next(d for d in summary["deltas"] if d.label == "CTR")
    assert ctr.current == pytest.approx(11 / 1001 * 100, rel=1e-6)


def test_position_is_impression_weighted(dashboard_db):
    """A single-impression day ranked 1st shouldn't drag the site average down
    from 20 to 10.5 — Search Console's own number is impression-weighted."""
    end = TODAY - timedelta(days=3)
    _add_days([
        (end, 0, 1, 1.0),
        (end - timedelta(days=1), 0, 999, 20.0),
    ])
    summary = site_summary(window_days=7, today=TODAY)
    position = next(d for d in summary["deltas"] if d.label == "Avg position")
    assert position.current == pytest.approx((1.0 + 999 * 20.0) / 1000, rel=1e-6)


def test_a_falling_position_reads_as_an_improvement(dashboard_db):
    """Position is the one metric where down is good. Rendering it like the
    rest would paint every genuine ranking gain red."""
    end = TODAY - timedelta(days=3)
    _add_days([(end, 0, 100, 4.0), (end - timedelta(days=7), 0, 100, 9.0)])
    summary = site_summary(window_days=7, today=TODAY)
    position = next(d for d in summary["deltas"] if d.label == "Avg position")
    assert position.direction == "down"
    assert position.good is True


def test_both_comparison_windows_end_on_settled_data(dashboard_db):
    """The current window must stop before Google's restatement window, or the
    dashboard invents a decline every single day."""
    summary = site_summary(window_days=28, today=TODAY)
    assert summary["current_window"][1] == TODAY - timedelta(days=3)
    # And the two windows must not overlap, or the "change" is partly a
    # comparison of a period against itself.
    assert summary["previous_window"][1] < summary["current_window"][0]


def test_the_series_marks_unsettled_days_as_provisional(dashboard_db):
    _add_days([(TODAY - timedelta(days=n), 1, 10, 5.0) for n in range(6)])
    series = site_series(days=10, today=TODAY)
    provisional = {p.day for p in series if p.provisional}
    assert provisional == {TODAY, TODAY - timedelta(days=1), TODAY - timedelta(days=2)}


def test_top_pages_carry_the_previous_window_for_comparison(dashboard_db):
    end = TODAY - timedelta(days=3)
    with get_session() as session:
        # 20 clicks this window, 5 last window.
        session.add(GscPageDaily(date=end, page="https://x/a", clicks=20,
                                 impressions=200, ctr=0.1, position=6.0))
        session.add(GscPageDaily(date=end - timedelta(days=7), page="https://x/a",
                                 clicks=5, impressions=100, ctr=0.05, position=9.0))
    rows = top_pages(window_days=7, limit=10, today=TODAY)
    assert len(rows) == 1
    assert rows[0]["current"].clicks == 20
    assert rows[0]["previous"].clicks == 5
    assert rows[0]["clicks_delta"] == 15
    assert rows[0]["position_delta"] == pytest.approx(-3.0)


def test_an_empty_database_reports_no_data_rather_than_zeros(dashboard_db):
    summary = site_summary(today=TODAY)
    assert summary["has_data"] is False
    assert summary["latest_day"] is None
