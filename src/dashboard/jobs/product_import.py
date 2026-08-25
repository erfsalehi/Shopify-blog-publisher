"""Push any unfinished product import along, unattended.

The run page advances a run while someone is watching it. This is what
finishes the job when nobody is: the owner pastes a collection URL, sees the
first products appear, closes the laptop, and the rest lands overnight.

Each tick keeps advancing runs until they finish or the tick runs out of
wall clock — bounded like every other job here, but not limited to a single
pass. One pass per tick would mean a forty-product import taking forty
scheduled runs to land, which on a nightly schedule is a week.

So: an import finishes in a minute or two if someone leaves the run page
open, within an hour or so if the hourly cron is wired up on the deployment
(`vercel.json`), and on the next nightly tick otherwise.
"""

from __future__ import annotations

import logging
import time

from dashboard import product_import
from dashboard.config import is_serverless
from dashboard.jobs.registry import JobResult, JobSpec, register

log = logging.getLogger(__name__)

#: Runs touched per tick. More than one because two imports queued back to
#: back shouldn't mean the second waits for the first to finish entirely.
MAX_RUNS_PER_TICK = 3

#: How long a tick keeps going before handing the function back. Passes are
#: individually bounded already; this bounds the sequence of them.
TICK_BUDGET_SECONDS = 45.0
LOCAL_TICK_BUDGET_SECONDS = 300.0


def advance_imports() -> JobResult:
    """Keep advancing unfinished imports until they're done or time is up.

    A loop rather than one pass per tick, because one pass per tick would
    mean a 40-product import taking 40 scheduled runs to finish — a week, on
    a nightly schedule. The loop is bounded by the same wall clock every
    other job on this deployment obeys, so the worst case is the tick
    returning with work left and the next one picking it up.
    """
    deadline = time.monotonic() + (
        TICK_BUDGET_SECONDS if is_serverless() else LOCAL_TICK_BUDGET_SECONDS
    )
    run_ids = product_import.active_run_ids(limit=MAX_RUNS_PER_TICK)
    if not run_ids:
        return JobResult(
            rows=0, skipped=True, skip_reason="No product imports are in progress."
        )

    passes = 0
    handled = 0
    stages: dict[int, str] = {}
    unfinished: list[int] = []
    for run_id in run_ids:
        while time.monotonic() < deadline:
            result = product_import.advance(run_id)
            passes += 1
            handled += result.handled
            stages[run_id] = result.stage
            if result.done:
                break
        else:
            unfinished.append(run_id)
            break
        if time.monotonic() >= deadline:
            unfinished.append(run_id)
            break
        log.info("import run %s finished at %s", run_id, stages.get(run_id))

    return JobResult(
        rows=handled,
        detail={
            "runs": [{"run": rid, "stage": stage} for rid, stage in stages.items()],
            "passes": passes,
            "still_running": unfinished,
        },
    )


register(
    JobSpec(
        name="product_import",
        title="Continue product imports",
        description=(
            "Advances any product import that still has work left, until it "
            "finishes or runs out of time. An import started on the "
            "Product Import page finishes here when nobody is watching the "
            "run page."
        ),
        fn=advance_imports,
        enabled_key="jobs.product_import.enabled",
        hour_key="jobs.product_import.hour",
        # Scraping a supplier's site through the proxy is exactly the case
        # the retry exists for, and a pass that dies partway costs only the
        # products it hadn't reached.
        max_attempts=2,
    )
)
