"""Vercel Cron endpoints — one HTTP call per scheduled job.

On `python -m dashboard`, APScheduler holds a timer inside one long-lived
process. Vercel has no such process: every request is its own short-lived
function invocation, so "run this at 06:00" has to become "Vercel calls this
URL at 06:00" instead. `vercel.json`'s `crons` array is the schedule; this
module is what answers the call.

**Auth here is a bearer token, not the owner's session.** Vercel Cron has no
browser and cannot carry the login cookie from `auth.py` — it authenticates
by sending `Authorization: Bearer <CRON_SECRET>` automatically whenever a
`CRON_SECRET` env var exists on the Vercel project (Vercel's own convention).
Checked independently of the password login, and refused outright if
`CRON_SECRET` isn't configured at all — an unauthenticated `/api/cron/*`
would let anyone on the internet trigger a paid DataForSEO call or a Shopify
write by requesting a URL.

**Jobs run synchronously, in the request.** The background-thread pattern
`web.py` uses for "Run now" doesn't survive here: Vercel can freeze or recycle
the process the instant a response is sent, so a job still running in a
background thread at that point may never finish. A cron endpoint calls
`run_job` directly and returns its result as the response body — the
function's execution time *is* the job's run time.

**That makes `maxDuration` a real constraint, not a formality.** The Search
Console job took 50s on this store's first backfill (27 API calls) before
settling to ~10s on later re-pull-only runs. `vercel.json` sets
`maxDuration: 60` for `/api/cron/*`, which is the most Vercel's Hobby tier
allows; Pro allows up to 300s. If a first deploy needs the full historical
backfill and 60s isn't enough, run it once from a machine with `python -m
dashboard` pointed at the same Postgres URL — the sync is idempotent and
resumable (see `jobs/windows.py`), so Vercel Cron picking up the remainder
the next day costs nothing.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Header, HTTPException

from dashboard.config import get_settings
from dashboard.jobs.registry import get_job
from dashboard.jobs.runner import JobAlreadyRunning, run_job

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cron", tags=["cron"])


def _check_cron_secret(authorization: str | None) -> None:
    settings = get_settings()
    if not settings.cron_configured:
        # Refuse rather than allow-if-unset: the equivalent local default
        # ("no password means no login required") is safe there because the
        # app is unreachable from anywhere else. A cron endpoint is reachable
        # from the whole internet the moment the app is deployed, so the
        # equivalent default here has to be the opposite.
        raise HTTPException(
            status_code=503,
            detail="CRON_SECRET is not configured — cron endpoints are "
                   "disabled until it is set on the Vercel project.",
        )
    expected = f"Bearer {settings.cron_secret}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing cron token.")


@router.get("/{job_name}")
def run_cron_job(job_name: str, authorization: str | None = Header(default=None)):
    _check_cron_secret(authorization)
    try:
        get_job(job_name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown job: {job_name}")

    try:
        run = run_job(job_name, trigger="cron")
    except JobAlreadyRunning:
        # Only reachable if two cron invocations somehow overlap (a retried
        # Vercel Cron trigger, a manual "Run now" mid-window). Reporting 409
        # rather than raising means the Vercel Cron dashboard sees the
        # invocation land, not an unexplained function error.
        return {"job": job_name, "started": False, "reason": "already running"}

    return {
        "job": job_name,
        "status": run.status,
        "rows": run.rows,
        "duration_ms": run.duration_ms,
        "attempts": run.attempts,
        "error": run.error,
        "detail": run.detail,
    }
