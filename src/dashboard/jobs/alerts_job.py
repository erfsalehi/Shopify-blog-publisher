"""Alert evaluation, as a job.

Registered like any other job so it shows up in the run log with the rest —
"when did the alerts last run" deserves the same answer as "when did the
Search Console sync last run", and a silent alerting system is worse than
none.

The runner also calls `evaluate` after every *other* job completes (see
`runner._evaluate_alerts`), which is what PLAN.md means by "evaluated after
each sync". This scheduled entry is the backstop for a day when nothing else
ran at all.
"""

from __future__ import annotations

from dashboard import alerts
from dashboard.jobs.registry import JobResult, JobSpec, register


def run_alert_rules() -> JobResult:
    summary = alerts.evaluate()
    return JobResult(rows=summary["new_alerts"], detail=summary)


register(
    JobSpec(
        name="alerts",
        title="Alert rules",
        description=(
            "Evaluates every enabled threshold rule against settled data and "
            "files what it finds in the alerts inbox. Also runs automatically "
            "after each of the other syncs."
        ),
        fn=run_alert_rules,
        enabled_key="jobs.alerts.enabled",
        hour_key="jobs.alerts.hour",
        # Reads only the local database — a failure here is a real bug, not
        # the proxy, so retrying would just delay the message.
        max_attempts=1,
    )
)
