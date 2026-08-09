"""Weekly advisor refresh.

Regenerates only the scopes whose note has gone stale, so a scheduled run
after a week of no change costs one call rather than six. Generation is
per-scope and failures are isolated: the free Gemini tier rate-limits per
model, and one scope hitting a cap must not stop the rest.
"""

from __future__ import annotations

import logging

from dashboard import advisor
from dashboard.jobs.registry import JobResult, JobSpec, register

log = logging.getLogger(__name__)


def refresh_advisor_notes() -> JobResult:
    from blog_pipeline.config import get_settings

    if not get_settings().has_google:
        return JobResult(
            skipped=True,
            skip_reason=(
                "No GOOGLE_API_KEY, so the advisor can't call Gemini. Get a "
                "free key at https://aistudio.google.com/apikey."
            ),
        )

    stale = advisor.stale_scopes(older_than_days=7)
    if not stale:
        return JobResult(rows=0, detail={"note": "every scope's note is current"})

    generated, failed = [], {}
    for scope in stale:
        note = advisor.generate(scope)
        if note.error:
            failed[scope] = note.error[:200]
        else:
            generated.append(scope)

    return JobResult(
        rows=len(generated),
        detail={
            "generated": generated,
            "failed": failed,
            "model": advisor_model(),
        },
    )


def advisor_model() -> str:
    from dashboard import store

    return store.get(store.ADVISOR_MODEL)


register(
    JobSpec(
        name="advisor_weekly",
        title="Advisor notes",
        description=(
            "Regenerates the per-tab advice for any area whose note is more "
            "than a week old. Reads only what's already in the database — the "
            "model is given no tools and can't fetch anything."
        ),
        fn=refresh_advisor_notes,
        enabled_key="jobs.advisor_weekly.enabled",
        hour_key="jobs.advisor_weekly.hour",
        # Free-tier rate limits are per-day, not transient. Retrying a 429
        # here would burn the remaining quota rather than recover.
        max_attempts=1,
    )
)
