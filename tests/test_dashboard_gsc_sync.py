"""The Search Console daily sync: what it asks for, and what it stores.

The properties under test are the two the job's whole design rests on — it is
idempotent, and it resumes — plus the two arithmetic traps that would produce
confidently wrong dashboards (double-counting on re-run, and freezing Google's
provisional restatements in place).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from dashboard.db import get_session
from dashboard.jobs.gsc import settled_through, sync_gsc_daily
from dashboard.jobs.windows import chunks, plan_window
from dashboard.models import GscFetchDay, GscPageDaily, GscSiteDaily

TODAY = date(2026, 8, 5)


class FakeGSC:
    """A Search Console that returns one row per day per requested dimension.

    Records every call so the tests can assert on how the job *asked*, which is
    where the resume and chunking behaviour actually lives — the stored rows
    look the same whether it re-fetched everything or nothing.
    """

    def __init__(self, *, enabled: bool = True, pages=("https://drflooring.ca/a",)):
        self.enabled = enabled
        self.site_url = "sc-domain:drflooring.ca"
        self.calls: list[dict] = []
        self._pages = pages

    def query(self, *, dimensions, start_date, end_date, row_limit=25000):
        self.calls.append(
            {
                "dimensions": list(dimensions),
                "start": start_date,
                "end": end_date,
                "row_limit": row_limit,
            }
        )
        rows = []
        day = start_date
        while day <= end_date:
            if dimensions == ["date"]:
                rows.append(
                    {
                        "keys": [day.isoformat()],
                        "clicks": 10,
                        "impressions": 100,
                        "ctr": 0.1,
                        "position": 8.0,
                    }
                )
            else:
                for page in self._pages:
                    rows.append(
                        {
                            "keys": [day.isoformat(), page],
                            "clicks": 2,
                            "impressions": 20,
                            "ctr": 0.1,
                            "position": 9.0,
                        }
                    )
            day += timedelta(days=1)
        return rows


def _count(model) -> int:
    with get_session() as session:
        return session.query(model).count()


# ── Window planning ────────────────────────────────────────────────


def test_first_run_plans_the_full_backfill():
    start, end, wanted = plan_window(
        set(), backfill_days=30, recent_days=10, today=TODAY
    )
    assert start == TODAY - timedelta(days=30)
    assert end == TODAY
    assert len(wanted) == 31  # inclusive of both ends


def test_later_runs_refetch_only_the_gap_plus_the_restatement_window():
    have = {TODAY - timedelta(days=n) for n in range(5, 31)}
    _, _, wanted = plan_window(
        have, backfill_days=30, recent_days=10, today=TODAY
    )
    # Days 0-4 were never fetched; days 5-10 are inside the re-pull window and
    # get fetched again because Google restates them.
    assert min(wanted) == TODAY - timedelta(days=10)
    assert max(wanted) == TODAY


def test_shrinking_the_backfill_setting_does_not_abandon_older_history():
    """A backfill lowered from 180 to 30 must not orphan the days already
    held: they'd stop being maintained while still being charted."""
    have = {TODAY - timedelta(days=n) for n in range(0, 100)}
    start, _, _ = plan_window(have, backfill_days=30, recent_days=10, today=TODAY)
    assert start == TODAY - timedelta(days=99)


# ── Chunking ───────────────────────────────────────────────────────


def test_chunks_are_consecutive_runs_capped_at_the_chunk_size():
    days = [date(2026, 8, 1) + timedelta(days=n) for n in range(10)]
    assert chunks(days, 4) == [
        (date(2026, 8, 1), date(2026, 8, 4)),
        (date(2026, 8, 5), date(2026, 8, 8)),
        (date(2026, 8, 9), date(2026, 8, 10)),
    ]


def test_a_gap_splits_a_chunk_rather_than_being_spanned():
    """Spanning a gap would silently re-fetch days we already have — the exact
    waste the fetch ledger exists to prevent."""
    days = [date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 9)]
    assert chunks(days, 7) == [
        (date(2026, 8, 1), date(2026, 8, 2)),
        (date(2026, 8, 9), date(2026, 8, 9)),
    ]


# ── The job ────────────────────────────────────────────────────────


def test_unconfigured_search_console_is_skipped_not_failed(dashboard_db):
    result = sync_gsc_daily(client=FakeGSC(enabled=False), today=TODAY)
    assert result.skipped is True
    assert "GSC_CREDENTIALS_JSON" in result.skip_reason
    assert _count(GscSiteDaily) == 0


def test_a_run_stores_site_totals_and_page_rows(dashboard_db, monkeypatch):
    monkeypatch.setattr(
        "dashboard.store.get",
        _stub_store({"gsc.backfill_days": 6, "gsc.recent_days": 3,
                     "gsc.page_chunk_days": 7, "gsc.page_row_limit": 60000}),
    )
    result = sync_gsc_daily(client=FakeGSC(), today=TODAY)
    assert _count(GscSiteDaily) == 7
    assert _count(GscPageDaily) == 7
    assert result.detail["truncated_chunks"] == []


def test_running_twice_does_not_double_count(dashboard_db, monkeypatch):
    monkeypatch.setattr(
        "dashboard.store.get",
        _stub_store({"gsc.backfill_days": 6, "gsc.recent_days": 3,
                     "gsc.page_chunk_days": 7, "gsc.page_row_limit": 60000}),
    )
    client = FakeGSC()
    sync_gsc_daily(client=client, today=TODAY)
    sync_gsc_daily(client=client, today=TODAY)
    # Same day count, not double. The runner retries whole jobs on a transient
    # proxy failure, so an appending sync would corrupt itself the first time
    # the proxy hiccupped mid-run.
    assert _count(GscSiteDaily) == 7
    assert _count(GscPageDaily) == 7


def test_the_second_run_refetches_only_the_restatement_window(
    dashboard_db, monkeypatch
):
    monkeypatch.setattr(
        "dashboard.store.get",
        _stub_store({"gsc.backfill_days": 30, "gsc.recent_days": 3,
                     "gsc.page_chunk_days": 7, "gsc.page_row_limit": 60000}),
    )
    client = FakeGSC()
    sync_gsc_daily(client=client, today=TODAY)
    first_calls = len(client.calls)
    client.calls.clear()

    sync_gsc_daily(client=client, today=TODAY)
    page_calls = [c for c in client.calls if c["dimensions"] == ["date", "page"]]
    assert len(client.calls) < first_calls
    # Only the four settling days, in one chunk.
    assert len(page_calls) == 1
    assert page_calls[0]["start"] == TODAY - timedelta(days=3)


def test_a_quiet_day_is_not_refetched_forever(dashboard_db, monkeypatch):
    """A day with no impressions returns no rows. Coverage is tracked in the
    fetch ledger precisely so that day doesn't look permanently missing."""
    monkeypatch.setattr(
        "dashboard.store.get",
        _stub_store({"gsc.backfill_days": 30, "gsc.recent_days": 3,
                     "gsc.page_chunk_days": 7, "gsc.page_row_limit": 60000}),
    )

    class Silent(FakeGSC):
        def query(self, **kwargs):
            super().query(**kwargs)
            return []

    client = Silent()
    sync_gsc_daily(client=client, today=TODAY)
    assert _count(GscSiteDaily) == 0
    with get_session() as session:
        assert session.query(GscFetchDay).filter(
            GscFetchDay.kind == "page"
        ).count() == 31

    client.calls.clear()
    sync_gsc_daily(client=client, today=TODAY)
    page_calls = [c for c in client.calls if c["dimensions"] == ["date", "page"]]
    assert len(page_calls) == 1  # the restatement window only, not all 31 days


def test_a_truncated_chunk_is_reported_and_left_unmarked(dashboard_db, monkeypatch):
    """Hitting the row cap means the chunk is incomplete. Storing it is fine;
    recording it as fetched would freeze partial data permanently."""
    monkeypatch.setattr(
        "dashboard.store.get",
        _stub_store({"gsc.backfill_days": 2, "gsc.recent_days": 1,
                     "gsc.page_chunk_days": 7, "gsc.page_row_limit": 5000}),
    )
    pages = tuple(f"https://drflooring.ca/p{n}" for n in range(2000))
    result = sync_gsc_daily(client=FakeGSC(pages=pages), today=TODAY)
    assert result.detail["truncated_chunks"]
    with get_session() as session:
        assert session.query(GscFetchDay).filter(
            GscFetchDay.kind == "page"
        ).count() == 0


def test_settled_through_excludes_googles_restatement_window():
    assert settled_through(TODAY) == TODAY - timedelta(days=3)


def _stub_store(values: dict):
    import dashboard.store as store

    real = store.get

    def fake(key):
        return values.get(key, real(key))

    return fake
