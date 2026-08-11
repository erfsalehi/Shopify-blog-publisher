"""Strategies: the plan, what it can actually do, and the baseline.

The advisor suggests; this commits. Three things carry the weight and are
pinned here, because each of them fails quietly rather than loudly:

  * **The baseline is frozen at activation, not at generation.** A baseline
    taken after the work started absorbs the change and reports that nothing
    happened.
  * **A step either runs or says it can't.** Nothing here can write to Google
    Business Profile or Google Ads, and a button that quietly did nothing
    would be the worst possible answer.
  * **A step the model returned malformed is downgraded, never offered.** An
    executable step with no payload is a button that fails on click.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from dashboard.db import get_session
from dashboard.models import (
    StepKind, Strategy, StrategyCheckpoint, StrategyStatus, StrategyStep,
)

TODAY = date(2026, 8, 11)


def _strategy(**kw) -> int:
    with get_session() as s:
        row = Strategy(goal=kw.pop("goal", "Rank higher in Langley"), **kw)
        s.add(row)
        s.flush()
        return row.id


def _step(strategy_id, kind, payload=None, **kw) -> int:
    with get_session() as s:
        row = StrategyStep(
            strategy_id=strategy_id, kind=kind,
            title=kw.pop("title", "A step"),
            payload_json=json.dumps(payload or {}), **kw
        )
        s.add(row)
        s.flush()
        return row.id


# ── What the model returns is not trusted verbatim ──────────────────


def test_an_executable_step_with_no_payload_is_downgraded(dashboard_db):
    """A blog_topic step with no topic is a Run button that fails on click.
    Presenting it as tracked is honest; presenting it as runnable is not."""
    from dashboard.strategy import _valid_step

    cleaned = _valid_step({"kind": "blog_topic", "title": "Write something"})

    assert cleaned["kind"] == StepKind.manual.value


def test_an_unknown_kind_becomes_manual_rather_than_being_dropped(dashboard_db):
    """Dropping it would lose a real instruction the owner should see;
    trusting it would route to a runner that doesn't exist."""
    from dashboard.strategy import _valid_step

    cleaned = _valid_step({"kind": "send_carrier_pigeon", "title": "Do a thing"})

    assert cleaned["kind"] == StepKind.manual.value
    assert cleaned["title"] == "Do a thing"


def test_a_step_with_no_title_is_discarded(dashboard_db):
    from dashboard.strategy import _valid_step

    assert _valid_step({"kind": "manual", "title": "   "}) is None


# ── What this app can and cannot do ─────────────────────────────────


@pytest.mark.parametrize("kind", ["gbp", "ads", "page_content", "manual"])
def test_untouchable_kinds_refuse_clearly(dashboard_db, kind):
    """Nothing in this project has write access to Google Business Profile or
    Google Ads. The refusal has to name that, because the alternative is a
    plan whose progress bar counts steps nobody did."""
    from dashboard import strategy

    sid = _strategy()
    step_id = _step(sid, kind)

    with pytest.raises(strategy.StepError, match="tracked, not automated"):
        strategy.execute(step_id)


def test_a_failing_step_records_why_instead_of_raising(dashboard_db):
    """The owner clicked a button on a page. The useful outcome is the page
    saying what went wrong, not a traceback."""
    from dashboard import strategy

    sid = _strategy()
    # A real kind, but an article id that doesn't exist.
    step_id = _step(sid, StepKind.article_refresh.value, {"article_id": 999999})

    step = strategy.execute(step_id)

    assert step.status == "failed"
    assert step.result


def test_a_blog_topic_step_queues_into_the_pipelines_own_calendar(dashboard_db):
    """Not a second path into Shopify — the same calendar `add-topic` writes
    to, so a strategy-queued article is reviewed like every other one."""
    from blog_pipeline.db.models import CalendarEntry
    from blog_pipeline.db.session import get_session as pipeline_session
    from blog_pipeline.db.session import init_db as init_pipeline

    from dashboard import strategy

    init_pipeline()
    sid = _strategy()
    step_id = _step(
        sid, StepKind.blog_topic.value,
        {"topic": "Underlayment for Langley condos",
         "keywords": ["underlayment langley"]},
    )

    step = strategy.execute(step_id)

    assert step.status == "done"
    with pipeline_session() as s:
        entry = s.query(CalendarEntry).one()
        assert entry.topic == "Underlayment for Langley condos"
        assert entry.target_keywords == ["underlayment langley"]


def test_running_a_done_step_twice_does_nothing(dashboard_db):
    """A double-submitted form must not queue the same article twice."""
    from blog_pipeline.db.models import CalendarEntry
    from blog_pipeline.db.session import get_session as pipeline_session
    from blog_pipeline.db.session import init_db as init_pipeline

    from dashboard import strategy

    init_pipeline()
    sid = _strategy()
    step_id = _step(sid, StepKind.blog_topic.value, {"topic": "Once only"})

    strategy.execute(step_id)
    strategy.execute(step_id)

    with pipeline_session() as s:
        assert s.query(CalendarEntry).count() == 1


# ── The baseline ────────────────────────────────────────────────────


def test_activation_freezes_the_baseline(dashboard_db):
    from dashboard import strategy

    sid = _strategy()
    row = strategy.activate(sid, TODAY)

    assert row.status == StrategyStatus.active.value
    assert row.baseline_at is not None
    assert row.baseline["as_of"] == TODAY.isoformat()


def test_activating_twice_does_not_move_the_baseline(dashboard_db):
    """Re-activating after the work started would replace the "before"
    numbers with "during" ones and quietly erase the evidence."""
    from dashboard import strategy

    sid = _strategy()
    first = strategy.activate(sid, TODAY)
    later = strategy.activate(sid, date(2026, 9, 1))

    assert later.baseline["as_of"] == first.baseline["as_of"] == TODAY.isoformat()


def test_a_draft_has_no_baseline_to_measure_against(dashboard_db):
    from dashboard.jobs.strategy_weekly import run_strategy_checkpoints

    _strategy()  # left as draft

    result = run_strategy_checkpoints(TODAY)

    assert result.skipped is True
    assert "active" in result.skip_reason


def test_a_checkpoint_records_step_progress_alongside_the_metrics(dashboard_db,
                                                                 monkeypatch):
    """"Nothing moved" means something different when none of the plan was
    done, so the count travels with the measurement rather than being looked
    up separately later."""
    from dashboard import strategy

    monkeypatch.setattr(strategy, "_narrate", lambda *a, **k: None)

    sid = _strategy()
    _step(sid, StepKind.manual.value, status="done")
    _step(sid, StepKind.manual.value)
    strategy.activate(sid, TODAY)

    row = strategy.checkpoint(sid, TODAY)

    assert row.steps_done == 1
    assert row.steps_total == 2
    assert row.metrics["as_of"] == TODAY.isoformat()


def test_a_second_checkpoint_on_one_day_replaces_the_first(dashboard_db,
                                                           monkeypatch):
    """Re-running the job must not produce two rows for one day, which would
    double every point on the series."""
    from dashboard import strategy

    monkeypatch.setattr(strategy, "_narrate", lambda *a, **k: None)

    sid = _strategy()
    strategy.activate(sid, TODAY)
    strategy.checkpoint(sid, TODAY)
    strategy.checkpoint(sid, TODAY)

    with get_session() as s:
        assert s.query(StrategyCheckpoint).filter(
            StrategyCheckpoint.strategy_id == sid
        ).count() == 1


def test_a_measurement_survives_the_model_being_unreachable(dashboard_db,
                                                            monkeypatch):
    """The narrative is the nice-to-have; the numbers are the point. Losing
    both because Gemini was rate-limited would throw away a week."""
    from dashboard import strategy

    def _boom(*a, **k):
        raise RuntimeError("429 rate limited")

    monkeypatch.setattr(strategy, "_call", _boom)

    sid = _strategy()
    strategy.activate(sid, TODAY)
    row = strategy.checkpoint(sid, TODAY)

    assert row.narrative is None
    assert row.metrics["as_of"] == TODAY.isoformat()


def test_a_generation_failure_is_stored_not_raised(dashboard_db, monkeypatch):
    """A dead model should leave a row explaining itself, not a 500 on the
    page the owner just submitted a goal from."""
    from dashboard import strategy

    def _boom(*a, **k):
        raise RuntimeError("no model available")

    monkeypatch.setattr(strategy, "_call", _boom)
    monkeypatch.setattr(strategy, "_brief", lambda *a, **k: "BRIEF")

    row = strategy.generate("Rank higher in Langley")

    assert row.error and "no model available" in row.error
    assert row.summary is None


def test_a_goal_is_required(dashboard_db):
    from dashboard import strategy

    with pytest.raises(ValueError, match="goal"):
        strategy.generate("   ")
