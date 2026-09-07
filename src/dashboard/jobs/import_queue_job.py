"""Start the next queued collection, once a day, unattended.

The other half of the product importer's automation. `product_import`
advances a run that already exists; this is what decides there should be one.

**It starts at most one collection, and only when nothing is importing.** Two
runs at once compete for the same bounded passes, so both crawl, and the
second range's collection and cross-linking stages interleave with the
first's. So the cadence is "one a day" for a range that fits in a day, and
"when the one in front finishes" for one that doesn't — which is the truth
rather than a promise the deployment can't keep.

Scheduled ahead of `product_import` so a collection started here gets its
first passes the same night rather than waiting a day for them.
"""

from __future__ import annotations

import logging

from dashboard import import_queue
from dashboard.jobs.registry import JobResult, JobSpec, register

log = logging.getLogger(__name__)


def start_next_import() -> JobResult:
    settled = import_queue.reconcile()
    started = import_queue.start_next()
    waiting = import_queue.waiting()

    if started is None:
        return JobResult(
            rows=0,
            skipped=True,
            skip_reason=(
                f"An import is still running; {waiting} waiting."
                if waiting
                else "Nothing is queued."
            ),
            detail={"waiting": waiting, "settled": settled},
        )

    log.info("queued import %s started as run %s", started["entry"], started["run"])
    return JobResult(
        rows=1,
        detail={
            "started": started["source_url"],
            "run": started["run"],
            "waiting": waiting,
            "settled": settled,
        },
    )


register(
    JobSpec(
        name="import_queue",
        title="Start the next queued import",
        description=(
            "Takes the next collection off the import queue and starts it, "
            "if nothing else is importing. One at a time: two runs at once "
            "would each get half the passes and finish in twice the time. "
            "Whatever it starts is carried on by 'Continue product imports'."
        ),
        fn=start_next_import,
        enabled_key="jobs.import_queue.enabled",
        hour_key="jobs.import_queue.hour",
        # Starting a run is a database write and a URL fetch away from
        # nothing having happened; there is no half-started import to clean
        # up, so a retry is free.
        max_attempts=3,
    )
)
