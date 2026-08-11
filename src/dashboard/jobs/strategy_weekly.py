"""Weekly measurement of every active strategy → `strategy_checkpoint`.

The half of a strategy that makes it more than a to-do list. Each run
compares today's numbers against the baseline frozen when the strategy was
activated, and writes one row per strategy per week.

Stored per week rather than recomputed on the page, because the value is the
series: "clicks are up 12%" means nothing without the four weeks before it,
and a number computed fresh each time can't show a week where things got
worse and then recovered.

Only `active` strategies are measured. A draft has no baseline to measure
against — measuring it would compare today with today.
"""

from __future__ import annotations

import logging
from datetime import date

from dashboard import strategy as strategy_mod
from dashboard.db import get_session
from dashboard.jobs.registry import JobResult, JobSpec, register
from dashboard.models import Strategy, StrategyStatus

log = logging.getLogger(__name__)


def run_strategy_checkpoints(today: date | None = None) -> JobResult:
    today = today or date.today()

    with get_session() as session:
        active = [
            (row.id, row.goal)
            for row in session.query(Strategy).filter(
                Strategy.status == StrategyStatus.active.value
            )
        ]

    if not active:
        return JobResult(
            skipped=True,
            skip_reason=(
                "No active strategies. Generate one on the Strategy page and "
                "activate it — that's what freezes the baseline."
            ),
        )

    measured = 0
    narrated = 0
    failures: list[str] = []
    for strategy_id, goal in active:
        try:
            row = strategy_mod.checkpoint(strategy_id, today)
            measured += 1
            if row.narrative:
                narrated += 1
        except Exception as exc:  # noqa: BLE001 - one strategy, not the run
            log.exception("checkpoint for strategy %s failed", strategy_id)
            failures.append(f"{goal[:40]}: {type(exc).__name__}: {exc}")

    detail = {
        "active_strategies": len(active),
        "measured": measured,
        "with_narrative": narrated,
    }
    if failures:
        detail["failures"] = failures[:5]
    return JobResult(rows=measured, detail=detail)


register(
    JobSpec(
        name="strategy_weekly",
        title="Strategy checkpoints",
        description=(
            "Measures every active strategy against the baseline frozen when "
            "it was activated, and writes a short read of what moved. This "
            "is what makes a plan answerable rather than aspirational."
        ),
        fn=run_strategy_checkpoints,
        enabled_key="jobs.strategy_weekly.enabled",
        hour_key="jobs.strategy_weekly.hour",
    )
)
