"""Running a job: logging, retry, and not running it twice at once.

The retry logic here exists for one specific reason. A local HTTP proxy on
this machine intermittently answers outbound requests with HTTP 429
(`local_rate_limited`) or drops the TLS connection mid-handshake. Neither is a
real failure — the same request succeeds seconds later — so a sync that gave
up on the first one would leave the dashboard stale most mornings for no
reason. What is *not* retried is equally deliberate: a 403 from Search Console
means the service account was never granted the property, and retrying that
three times just delays a message the owner needs to read.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from dashboard.db import get_session
from dashboard.jobs.registry import JobResult, JobSpec, get_job
from dashboard.models import JobRun, JobStatus
from dashboard import store

log = logging.getLogger(__name__)

# One lock per job name. A manual "Run now" while the scheduler is mid-sync
# would otherwise have two threads upserting the same rows.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

# Substrings that mark a proxy failure rather than a real one. Matched against
# the exception text because the proxy reports them in wildly different shapes
# depending on where the connection died.
_TRANSIENT_MARKERS = (
    "local_rate_limited",
    "ssl",
    "eof occurred",
    "connection reset",
    "connection aborted",
    "timed out",
    "temporarily unavailable",
)


class JobAlreadyRunning(RuntimeError):
    pass


def _lock_for(name: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(name, threading.Lock())


def is_transient(exc: BaseException) -> bool:
    """Whether retrying this exception could plausibly work.

    Errs toward not retrying: an unknown exception is treated as real, so a
    genuine bug surfaces on the jobs page instead of being retried into a
    slower version of itself.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        # 429 and 5xx are the retryable ones. Note 429 here is usually the
        # local proxy rather than Google — Google's own quota errors come back
        # as 403 with a reason, which we deliberately don't retry.
        return code == 429 or 500 <= code < 600
    if isinstance(exc, (httpx.TransportError, TimeoutError, ConnectionError)):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _backoff_seconds(attempt: int) -> float:
    """2s, 4s, 8s ... with jitter, capped. Jitter matters because the proxy
    rate-limits by window: retrying on an exact schedule can land every
    attempt in the same bad window."""
    base = min(2.0 * (2 ** (attempt - 1)), 30.0)
    return base * (0.75 + random.random() * 0.5)


def _prune_history(session, job: str) -> None:
    keep = store.get(store.JOB_HISTORY_KEEP)
    ids = [
        row_id
        for (row_id,) in session.query(JobRun.id)
        .filter(JobRun.job == job)
        .order_by(JobRun.started_at.desc(), JobRun.id.desc())
        .offset(keep)
        .all()
    ]
    if ids:
        session.query(JobRun).filter(JobRun.id.in_(ids)).delete(
            synchronize_session=False
        )


def run_job(
    name: str,
    *,
    trigger: str = "manual",
    sleep: "callable" = time.sleep,
) -> JobRun:
    """Run one registered job to completion and return its `job_run` row.

    Never raises for a job that failed — the failure is the row's `error`, and
    that is the whole point of the jobs page. It does raise `JobAlreadyRunning`
    if the job is already in flight, since that's a caller error, not a run.

    `sleep` is injected so tests can exercise the retry path without waiting.
    """
    spec = get_job(name)
    lock = _lock_for(name)
    if not lock.acquire(blocking=False):
        raise JobAlreadyRunning(f"{name} is already running")
    try:
        return _run_locked(spec, trigger=trigger, sleep=sleep)
    finally:
        lock.release()


def is_running(name: str) -> bool:
    lock = _lock_for(name)
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


def _run_locked(spec: JobSpec, *, trigger: str, sleep) -> JobRun:
    started = datetime.now(timezone.utc)
    with get_session() as session:
        run = JobRun(
            job=spec.name,
            status=JobStatus.running.value,
            started_at=started,
            trigger=trigger,
        )
        session.add(run)
        session.flush()
        run_id = run.id

    began = time.monotonic()
    result: JobResult | None = None
    error: BaseException | None = None
    attempts = 0

    for attempt in range(1, spec.max_attempts + 1):
        attempts = attempt
        try:
            result = spec.fn()
            error = None
            break
        except Exception as exc:  # noqa: BLE001 - the runner's whole job
            error = exc
            if attempt >= spec.max_attempts or not is_transient(exc):
                break
            wait = _backoff_seconds(attempt)
            log.warning(
                "job %s attempt %d/%d failed transiently (%s); retrying in %.1fs",
                spec.name, attempt, spec.max_attempts, exc, wait,
            )
            sleep(wait)

    duration_ms = int((time.monotonic() - began) * 1000)

    with get_session() as session:
        run = session.get(JobRun, run_id)
        run.finished_at = datetime.now(timezone.utc)
        run.duration_ms = duration_ms
        run.attempts = attempts
        if error is not None:
            run.status = JobStatus.error.value
            run.error = f"{type(error).__name__}: {error}"
            log.error("job %s failed: %s", spec.name, run.error)
        else:
            assert result is not None
            run.rows = result.rows
            run.detail_json = json.dumps(result.detail, default=str)
            if result.skipped:
                run.status = JobStatus.skipped.value
                run.error = result.skip_reason
            else:
                run.status = JobStatus.ok.value
        _prune_history(session, spec.name)
        session.flush()
        session.expunge(run)

    _evaluate_alerts(spec.name)
    return run


def _evaluate_alerts(job_name: str) -> None:
    """Re-check the alert rules after a sync changes the data.

    PLAN.md wants rules "evaluated after each sync", and running them here
    rather than only on a schedule means a failed 06:00 sync produces its
    alert at 06:00, not at 08:00. Guarded against the alerts job itself for
    the obvious reason.

    Deliberately swallows its own failures: a bug in a rule must not turn a
    successful sync into a failed one on the jobs page.
    """
    if job_name == "alerts":
        return
    try:
        from dashboard import alerts

        alerts.evaluate()
    except Exception:  # noqa: BLE001
        log.exception("alert evaluation after job %s failed", job_name)


def run_in_background(name: str, *, trigger: str = "manual") -> threading.Thread:
    """Fire a job off the request thread so "Run now" returns immediately.

    A GSC backfill takes minutes; holding the HTTP response open for it would
    look like a hung page. The jobs page polls the run log instead.
    """
    thread = threading.Thread(
        target=_run_and_swallow, args=(name, trigger), name=f"job-{name}", daemon=True
    )
    thread.start()
    return thread


def _run_and_swallow(name: str, trigger: str) -> None:
    try:
        run_job(name, trigger=trigger)
    except JobAlreadyRunning:
        log.info("job %s already running; ignoring duplicate trigger", name)
    except Exception:  # pragma: no cover - run_job records its own failures
        log.exception("job %s crashed outside the runner", name)


#: How long a `running` row is given before it's assumed dead. Well above
#: Vercel's 60s function ceiling, and above the slowest honest local job (a
#: GSC backfill runs for minutes), so this can only ever catch a corpse.
STALE_RUN_AFTER = timedelta(minutes=15)


def reap_stale_runs(older_than: timedelta = STALE_RUN_AFTER) -> int:
    """Close out `running` rows whose process is gone. Returns how many.

    A run is marked `running` in one transaction and resolved in another, so
    anything that kills the process in between leaves the row saying
    "running" forever — the Jobs page then shows a job permanently in flight
    that nothing will ever finish.

    Rare locally (it needs a crash or a Ctrl-C at the wrong moment) and
    routine on Vercel, where a function is frozen the instant it responds and
    any work still on a background thread simply stops. The in-process lock
    dies with the process, so this is cosmetic rather than blocking — but a
    status display that lies is its own bug.

    Age-based rather than "mark everything running at startup": Vercel runs
    concurrent invocations, and a cron job legitimately in flight in another
    one must not be declared dead by this one booting.
    """
    cutoff = datetime.now(timezone.utc) - older_than
    with get_session() as session:
        stale = (
            session.query(JobRun)
            .filter(JobRun.status == JobStatus.running.value)
            .filter(JobRun.started_at < cutoff)
            .all()
        )
        for row in stale:
            row.status = JobStatus.error.value
            row.error = (
                "Interrupted — the process running this job stopped before it "
                "could report a result."
            )
        return len(stale)


def last_runs() -> dict[str, JobRun]:
    """Most recent run per job name."""
    out: dict[str, JobRun] = {}
    with get_session() as session:
        rows = (
            session.query(JobRun)
            .order_by(JobRun.started_at.desc(), JobRun.id.desc())
            .all()
        )
        for row in rows:
            out.setdefault(row.job, row)
        for row in out.values():
            session.expunge(row)
    return out


def recent_runs(limit: int = 50, job: str | None = None) -> list[JobRun]:
    with get_session() as session:
        query = session.query(JobRun)
        if job:
            query = query.filter(JobRun.job == job)
        rows = (
            query.order_by(JobRun.started_at.desc(), JobRun.id.desc())
            .limit(limit)
            .all()
        )
        for row in rows:
            session.expunge(row)
    return rows
