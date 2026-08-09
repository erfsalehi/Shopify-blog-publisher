"""In-process APScheduler wiring.

Scope note: this schedules the *dashboard's* jobs only. The existing Wednesday
blog-refresh cron keeps running exactly where it is — PLAN.md says migrate it
later, and the one thing worse than two schedulers is a half-moved one.

Schedules are read from the settings store at start and re-read whenever the
owner saves the settings page (`reschedule()`), so changing the nightly hour
doesn't need a restart.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from dashboard import store
from dashboard.jobs import all_jobs
from dashboard.jobs.registry import JobSpec
from dashboard.jobs.runner import run_job, JobAlreadyRunning

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _fire(name: str) -> None:
    try:
        run_job(name, trigger="scheduler")
    except JobAlreadyRunning:
        # The owner clicked Run now and it's still going. Skipping is right:
        # the data this run would have fetched is being fetched already.
        log.info("scheduled run of %s skipped — already running", name)


def _schedule_for(spec: JobSpec) -> CronTrigger | None:
    """None for a manual-only job, or one the owner has switched off."""
    if not spec.enabled_key or not spec.hour_key:
        return None
    if not store.get(spec.enabled_key):
        return None
    return CronTrigger(hour=store.get(spec.hour_key), minute=spec.minute)


def _apply(scheduler: BackgroundScheduler) -> list[str]:
    scheduled: list[str] = []
    for spec in all_jobs():
        job_id = f"job:{spec.name}"
        existing = scheduler.get_job(job_id)
        trigger = _schedule_for(spec)
        if trigger is None:
            if existing:
                scheduler.remove_job(job_id)
            continue
        if existing:
            scheduler.reschedule_job(job_id, trigger=trigger)
        else:
            scheduler.add_job(
                _fire,
                trigger=trigger,
                id=job_id,
                args=[spec.name],
                # A laptop that was asleep at 6am shouldn't fire four backlogged
                # runs when it wakes; coalesce them into one, and only if the
                # wake-up is within the hour.
                coalesce=True,
                misfire_grace_time=3600,
                max_instances=1,
            )
        scheduled.append(spec.name)
    return scheduled


def start() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone=None)  # machine local time
        _scheduler.start()
    names = _apply(_scheduler)
    log.info("scheduler running; scheduled jobs: %s", ", ".join(names) or "none")
    return _scheduler


def reschedule() -> None:
    """Re-read schedules from the settings store. Called after a settings save."""
    if _scheduler is not None:
        _apply(_scheduler)


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def next_run_times() -> dict[str, object]:
    """Job name → next fire time, for the jobs page. Empty when not running."""
    if _scheduler is None:
        return {}
    out = {}
    for job in _scheduler.get_jobs():
        if job.id.startswith("job:"):
            out[job.id[4:]] = job.next_run_time
    return out
