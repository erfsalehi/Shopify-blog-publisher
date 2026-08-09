"""The job runner: what gets logged, what gets retried, what doesn't.

The retry policy is the interesting part. It exists for one specific
environment — a local proxy that answers at random with 429 or a dropped TLS
connection — and the failure mode to guard against is not "doesn't retry
enough" but "retries a real error", which turns a clear message into three
identical ones arriving two minutes late.
"""

from __future__ import annotations

import httpx
import pytest

from dashboard.db import get_session
from dashboard.jobs.registry import JobResult, JobSpec
from dashboard.jobs.runner import (
    JobAlreadyRunning,
    _LOCKS,
    is_transient,
    last_runs,
    run_job,
)
from dashboard.models import JobRun, JobStatus


@pytest.fixture
def registry(monkeypatch):
    """An isolated job registry so tests can't see each other's jobs."""
    jobs: dict[str, JobSpec] = {}
    monkeypatch.setattr("dashboard.jobs.runner.get_job", lambda name: jobs[name])
    _LOCKS.clear()
    return jobs


def _add(registry, name, fn, **kwargs):
    spec = JobSpec(name=name, title=name, description="", fn=fn, **kwargs)
    registry[name] = spec
    return spec


# ── Transient classification ───────────────────────────────────────


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("ssl: EOF occurred in violation of protocol"),
        httpx.ReadTimeout("timed out"),
        RuntimeError("local_rate_limited"),
        httpx.HTTPStatusError(
            "429", request=httpx.Request("GET", "http://x"),
            response=httpx.Response(429),
        ),
        httpx.HTTPStatusError(
            "503", request=httpx.Request("GET", "http://x"),
            response=httpx.Response(503),
        ),
    ],
)
def test_proxy_style_failures_are_transient(exc):
    assert is_transient(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        # The 403 is the one that matters: it means the service account was
        # never granted the property, and retrying only delays that message.
        httpx.HTTPStatusError(
            "403", request=httpx.Request("GET", "http://x"),
            response=httpx.Response(403),
        ),
        ValueError("GSC_CREDENTIALS_JSON is not valid JSON"),
        KeyError("keys"),
    ],
)
def test_real_failures_are_not_retried(exc):
    assert is_transient(exc) is False


# ── Logging ────────────────────────────────────────────────────────


def test_a_successful_run_records_rows_and_detail(dashboard_db, registry):
    _add(registry, "ok", lambda: JobResult(rows=42, detail={"window": "a..b"}))
    run = run_job("ok")
    assert run.status == JobStatus.ok.value
    assert run.rows == 42
    assert run.detail == {"window": "a..b"}
    assert run.attempts == 1
    assert run.finished_at is not None


def test_a_skipped_run_is_not_an_error(dashboard_db, registry):
    _add(registry, "skip", lambda: JobResult(skipped=True, skip_reason="no key"))
    run = run_job("skip")
    assert run.status == JobStatus.skipped.value
    assert run.error == "no key"


def test_a_failing_job_is_recorded_rather_than_raised(dashboard_db, registry):
    def boom():
        raise ValueError("bad property string")

    _add(registry, "boom", boom, max_attempts=1)
    run = run_job("boom")  # must not raise — the jobs page is the report
    assert run.status == JobStatus.error.value
    assert "bad property string" in run.error


def test_a_transient_failure_is_retried_and_the_attempts_are_visible(
    dashboard_db, registry
):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("local_rate_limited")
        return JobResult(rows=1)

    _add(registry, "flaky", flaky, max_attempts=3)
    run = run_job("flaky", sleep=lambda _: None)
    assert run.status == JobStatus.ok.value
    # Surfacing the retry count is the point: a green run that took three
    # attempts is the proxy misbehaving, and the owner should be able to see it.
    assert run.attempts == 3


def test_a_real_error_is_not_retried_even_when_attempts_remain(
    dashboard_db, registry
):
    calls = {"n": 0}

    def broken():
        calls["n"] += 1
        raise ValueError("403 — service account not on the property")

    _add(registry, "broken", broken, max_attempts=5)
    run = run_job("broken", sleep=lambda _: None)
    assert calls["n"] == 1
    assert run.attempts == 1
    assert run.status == JobStatus.error.value


def test_a_job_cannot_run_twice_at_once(dashboard_db, registry):
    def reentrant():
        with pytest.raises(JobAlreadyRunning):
            run_job("reentrant")
        return JobResult(rows=1)

    _add(registry, "reentrant", reentrant)
    assert run_job("reentrant").status == JobStatus.ok.value


def test_history_is_pruned_to_the_configured_depth(dashboard_db, registry, monkeypatch):
    import dashboard.store as store

    monkeypatch.setattr(store, "get", lambda key: 3 if key == store.JOB_HISTORY_KEEP
                        else 0)
    _add(registry, "spam", lambda: JobResult(rows=1))
    for _ in range(6):
        run_job("spam")
    with get_session() as session:
        assert session.query(JobRun).filter(JobRun.job == "spam").count() == 3


def test_last_runs_reports_the_most_recent_per_job(dashboard_db, registry):
    _add(registry, "a", lambda: JobResult(rows=1))
    _add(registry, "b", lambda: JobResult(rows=2))
    run_job("a")
    run_job("b")
    run_job("a")
    latest = last_runs()
    assert set(latest) == {"a", "b"}
    assert latest["a"].rows == 1
