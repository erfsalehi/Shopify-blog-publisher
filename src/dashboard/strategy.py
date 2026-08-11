"""Goals, plans, and whether they worked.

The advisor reads the present and suggests things. This is the other half:
a commitment with a baseline, a plan made of steps this app can actually
carry out, and a weekly measurement against the numbers as they stood when
the work started.

Three things make that more than a to-do list:

  * **The baseline is captured at activation, not at generation.** Measuring
    against numbers taken after the work began would absorb the change into
    the baseline and report that nothing happened.
  * **Steps are typed, and the executable ones run existing code.** Queuing a
    blog topic goes through the pipeline's own calendar; a refresh goes
    through the refresh agent and produces a diff for review. Nothing here
    invents a second path to Shopify.
  * **The plan is checked against its own brief.** Same grounding check the
    advisor uses — a figure in the plan that isn't in the data is flagged,
    not displayed as fact.

What this deliberately cannot do is act on Google Business Profile or Google
Ads, because nothing in this project has write access to either. Those come
back as tracked steps with a due date, which is honest, rather than as
buttons that quietly do nothing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from dashboard import store
from dashboard.advisor import _call, _clean_json, unverified_figures
from dashboard.db import get_session
from dashboard.models import (
    EXECUTABLE_KINDS,
    StepKind,
    Strategy,
    StrategyCheckpoint,
    StrategyStatus,
    StrategyStep,
)

log = logging.getLogger(__name__)

_SYSTEM = """You are the strategist for D&R Flooring, a flooring retailer and \
installer with one showroom in Langley, British Columbia. Nothing is bought on \
their website — a phone call is the outcome.

You are given a GOAL and a BRIEF containing every figure this business has. \
Produce a concrete plan.

Rules, all of them load-bearing:

* Use ONLY figures from the BRIEF. Do not estimate, extrapolate or recall \
numbers from anywhere else. If something needed isn't there, say so in the \
reading and plan around not knowing it.
* Every step must be specific enough to do tomorrow. "Improve local SEO" is \
not a step. "Queue an article on acoustic underlayment for Langley condos, \
targeting 'underlayment langley'" is.
* Prefer steps the system can carry out. Their `kind` values:
  - blog_topic: queue an article. payload {"topic": "...", "keywords": ["..."]}
  - article_refresh: rewrite an existing post. payload {"article_id": N}
  - product_seo: change a product's title/description. payload \
{"product_id": N, "seo_title": "...", "seo_description": "..."}
  - page_content: a website change a human must make. payload {"url": "..."}
  - ads: a Google Ads change. payload {}
  - gbp: a Google Business Profile change. payload {}
  - manual: anything else. payload {}
* Only use article_refresh or product_seo with an id that appears in the \
BRIEF. Never invent one.
* The local pack is ranked on proximity, reviews and Google Business Profile \
completeness. No website or content change moves it. If the goal involves the \
pack, the steps for it must be `gbp`, not content.

Reply with JSON only:
{"summary": "one sentence on the plan",
 "reading": "2-4 sentences on what the data says about this goal",
 "steps": [{"kind": "...", "title": "...", "rationale": "...", \
"payload": {...}}]}
"""


@dataclass(frozen=True)
class Plan:
    summary: str
    reading: str
    steps: list[dict]


def _brief(target: str | None, today: date | None = None) -> str:
    """Every scope's context in one document.

    A strategy that only saw one tab would recommend blog work for a pricing
    problem. The cost is a long prompt; the alternative is a strategist
    reasoning about a business it can only see a sixth of.
    """
    from dashboard.advisor import SCOPES, SCOPE_TITLES
    from dashboard.advisor_context import build_context

    today = today or date.today()
    parts: list[str] = []
    for scope in SCOPES:
        try:
            parts.append(
                f"===== {SCOPE_TITLES[scope].upper()} =====\n"
                + build_context(scope, today)
            )
        except Exception as exc:  # noqa: BLE001 - one dead scope, not the plan
            log.warning("strategy brief: scope %s failed (%s)", scope, exc)
            parts.append(f"===== {scope.upper()} =====\n(unavailable: {exc})")
    if target:
        parts.append(
            f"===== TARGET =====\nThis strategy is specifically about: {target}"
        )
    return "\n\n".join(parts)


def _valid_step(raw: dict) -> dict | None:
    kind = str(raw.get("kind") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not title:
        return None
    if kind not in {k.value for k in StepKind}:
        kind = StepKind.manual.value
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    # An executable step with no payload can't execute, and offering a button
    # that fails on click is worse than presenting it as a tracked item.
    if kind in EXECUTABLE_KINDS and not payload:
        kind = StepKind.manual.value
    return {
        "kind": kind,
        "title": title[:400],
        "rationale": str(raw.get("rationale") or "").strip() or None,
        "payload": payload,
    }


def generate(goal: str, *, target: str | None = None,
             model: str | None = None) -> Strategy:
    """Turn a goal into a stored plan. Draft until the owner activates it."""
    goal = (goal or "").strip()
    if not goal:
        raise ValueError("A strategy needs a goal.")

    model = model or store.get(store.ADVISOR_MODEL)
    context = _brief(target)
    prompt = (
        f"GOAL: {goal}\n"
        + (f"TARGET: {target}\n" if target else "")
        + f"\nBRIEF (the only figures that exist):\n{context}\n"
    )

    strategy = Strategy(
        goal=goal, target=target or None, model=model, context_md=context
    )
    steps: list[dict] = []
    try:
        text, _usage, used_model = _call(model, _SYSTEM, prompt)
        strategy.model = used_model
        parsed = _clean_json(text)
        strategy.summary = str(parsed.get("summary") or "").strip() or None
        strategy.reading = str(parsed.get("reading") or "").strip() or None
        for raw in (parsed.get("steps") or [])[:12]:
            if isinstance(raw, dict):
                cleaned = _valid_step(raw)
                if cleaned:
                    steps.append(cleaned)
        checked = " ".join(
            [strategy.summary or "", strategy.reading or ""]
            + [s["title"] + " " + (s["rationale"] or "") for s in steps]
        )
        strategy.unverified_json = json.dumps(unverified_figures(checked, context))
    except Exception as exc:  # noqa: BLE001 - surfaced on the row
        log.exception("strategy generation failed")
        strategy.error = f"{type(exc).__name__}: {exc}"

    with get_session() as session:
        session.add(strategy)
        session.flush()
        for position, step in enumerate(steps):
            session.add(StrategyStep(
                strategy_id=strategy.id,
                kind=step["kind"],
                title=step["title"],
                rationale=step["rationale"],
                payload_json=json.dumps(step["payload"]),
                position=position,
            ))
        session.flush()
        session.expunge(strategy)
    return strategy


# ── Measurement ─────────────────────────────────────────────────────


def snapshot(target: str | None = None, today: date | None = None) -> dict:
    """The numbers a strategy is judged on.

    Deliberately a small, fixed set. A snapshot that captured everything
    would make every checkpoint "12 things moved a bit" — which is not an
    answer to "did this work".
    """
    from dashboard import reporting

    today = today or date.today()
    out: dict = {"as_of": today.isoformat()}

    site = reporting.site_summary(window_days=28, today=today)
    out["clicks_28d"] = site["current"].clicks
    out["impressions_28d"] = site["current"].impressions
    out["ctr_28d"] = round(site["current"].ctr * 100, 3)
    out["position_28d"] = round(site["current"].position, 2)

    try:
        local = reporting.local_seo(days=28, today=today)
        out["home_sessions_28d"] = local["home_sessions"]
        out["home_conversions_28d"] = local["home_conversions"]
        out["home_share_pct"] = round(local["home_share"], 2)
        if local["has_ranks"]:
            out["local_pack_present"] = (
                local["pack_measured"] - local["pack_absent_count"]
            )
            out["local_pack_measured"] = local["pack_measured"]
            positions = [
                r.position
                for rows in local["ranks_by_keyword"].values()
                for r in rows
                if r.position
            ]
            if positions:
                out["local_organic_avg"] = round(sum(positions) / len(positions), 2)
    except Exception as exc:  # noqa: BLE001 - a missing source isn't a failure
        log.info("strategy snapshot: local metrics unavailable (%s)", exc)

    # If the strategy names a city, carry that city's own numbers too — the
    # aggregate "home" figure can hide a win in one of the two Langleys.
    if target:
        from sqlalchemy import func

        from dashboard.models import Ga4CityDaily

        since = today - timedelta(days=27)
        with get_session() as session:
            row = (
                session.query(
                    func.coalesce(func.sum(Ga4CityDaily.sessions), 0),
                    func.coalesce(func.sum(Ga4CityDaily.conversions), 0),
                )
                .filter(
                    Ga4CityDaily.date >= since,
                    Ga4CityDaily.city.ilike(f"%{target}%"),
                )
                .one()
            )
        out["target_sessions_28d"] = int(row[0])
        out["target_conversions_28d"] = int(row[1])
    return out


def activate(strategy_id: int, today: date | None = None) -> Strategy:
    """Commit to a plan and freeze the numbers it will be judged against."""
    with get_session() as session:
        strategy = session.get(Strategy, strategy_id)
        if strategy is None:
            raise KeyError(f"no strategy {strategy_id}")
        if strategy.status == StrategyStatus.active.value:
            session.expunge(strategy)
            return strategy
        target = strategy.target
    baseline = snapshot(target, today)
    with get_session() as session:
        strategy = session.get(Strategy, strategy_id)
        strategy.status = StrategyStatus.active.value
        strategy.baseline_json = json.dumps(baseline)
        strategy.baseline_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.flush()
        session.expunge(strategy)
    return strategy


def _narrate(strategy: Strategy, baseline: dict, current: dict,
             steps_done: int, steps_total: int) -> str | None:
    """One paragraph on what moved. None when the model isn't reachable —
    the measurement is worth keeping either way."""
    lines = ["METRIC | BASELINE | NOW"]
    for key, now in current.items():
        if key == "as_of":
            continue
        before = baseline.get(key)
        if before is None:
            continue
        lines.append(f"{key} | {before} | {now}")
    body = (
        f"GOAL: {strategy.goal}\n"
        f"Baseline taken {strategy.baseline_at:%Y-%m-%d}. "
        f"{steps_done} of {steps_total} planned steps are done.\n\n"
        + "\n".join(lines)
    )
    system = (
        "You report on whether a strategy is working, for the owner of a "
        "flooring showroom in Langley BC. Use ONLY the numbers given. Two to "
        "four sentences. Say plainly if nothing moved, or if it is too early "
        "to tell — a fabricated improvement is worse than no report. Do not "
        "recommend anything; this is a measurement, not advice."
    )
    try:
        text, _usage, _model = _call(store.get(store.ADVISOR_MODEL), system, body)
        return text.strip() or None
    except Exception as exc:  # noqa: BLE001
        log.info("strategy narrative unavailable (%s)", exc)
        return None


def checkpoint(strategy_id: int, today: date | None = None) -> StrategyCheckpoint:
    """Measure one active strategy against its baseline, once."""
    today = today or date.today()
    with get_session() as session:
        strategy = session.get(Strategy, strategy_id)
        if strategy is None:
            raise KeyError(f"no strategy {strategy_id}")
        target, baseline = strategy.target, strategy.baseline
        steps = session.query(StrategyStep).filter(
            StrategyStep.strategy_id == strategy_id
        ).all()
        steps_total = len(steps)
        steps_done = sum(1 for s in steps if s.status == "done")
        session.expunge(strategy)

    current = snapshot(target, today)
    narrative = _narrate(strategy, baseline, current, steps_done, steps_total)

    with get_session() as session:
        session.query(StrategyCheckpoint).filter(
            StrategyCheckpoint.strategy_id == strategy_id,
            StrategyCheckpoint.date == today,
        ).delete(synchronize_session=False)
        row = StrategyCheckpoint(
            strategy_id=strategy_id,
            date=today,
            metrics_json=json.dumps(current),
            narrative=narrative,
            steps_done=steps_done,
            steps_total=steps_total,
        )
        session.add(row)
        session.flush()
        session.expunge(row)
    return row


# ── Execution ───────────────────────────────────────────────────────


class StepError(RuntimeError):
    """A step couldn't be carried out. Carries a message for the owner."""


def _do_blog_topic(payload: dict) -> str:
    """Queue an article, through the pipeline's own calendar.

    The same path `blog-pipeline add-topic` takes, Linear issue and all, so a
    strategy-queued topic is reviewed exactly like every other article rather
    than arriving through a side door.
    """
    from blog_pipeline.db.models import CalendarEntry, EntryStatus, TopicSource
    from blog_pipeline.db.session import get_session as pipeline_session
    from blog_pipeline.graphs.calendar_graph import _get_or_create_calendar

    topic = str(payload.get("topic") or "").strip()
    if not topic:
        raise StepError("This step has no topic to queue.")
    keywords = [str(k).strip() for k in (payload.get("keywords") or []) if str(k).strip()]

    with pipeline_session() as session:
        calendar = _get_or_create_calendar(session)
        session.add(CalendarEntry(
            calendar_id=calendar.id,
            scheduled_date=date.today(),
            topic=topic[:500],
            target_keywords=keywords,
            source=TopicSource.manual,
            status=EntryStatus.queued,
            notes=str(payload.get("rationale") or "Queued from a strategy.")[:2000],
        ))
    return f"Queued '{topic}'" + (f" targeting {', '.join(keywords)}" if keywords else "")


def _do_article_refresh(payload: dict) -> str:
    """Produce a refresh diff. Deliberately does NOT publish.

    The refresh agent rewrites a live public page, and a strategy step that
    silently changed one would be the single most dangerous button in this
    app. It stops at a proposal the owner reviews on the article page.
    """
    from dashboard import refresh

    try:
        article_id = int(payload.get("article_id"))
    except (TypeError, ValueError):
        raise StepError("This step has no article id to refresh.") from None
    proposal = refresh.preview(article_id)
    return (
        f"Refresh drafted for article {article_id} — review and apply it on "
        f"the article page (proposal {proposal.id})."
    )


def _do_product_seo(payload: dict) -> str:
    from blog_pipeline.tools.shopify import ShopifyClient
    from dashboard.models import ShopifyProduct
    from dashboard.product_seo import write_seo

    try:
        product_id = int(payload.get("product_id"))
    except (TypeError, ValueError):
        raise StepError("This step has no product id.") from None
    title = payload.get("seo_title")
    description = payload.get("seo_description")
    if not title and not description:
        raise StepError("This step has no SEO title or description to write.")

    with get_session() as session:
        product = session.get(ShopifyProduct, product_id)
        if product is None:
            raise StepError(f"No product {product_id} in the catalogue snapshot.")
        gid, name = product.product_gid, product.title

    client = ShopifyClient()
    try:
        result = write_seo(
            client, gid,
            seo_title=title or None,
            seo_description=description or None,
        )
    finally:
        client.close()
    return f"SEO updated on {name}: {result['after']}"


_RUNNERS = {
    StepKind.blog_topic.value: _do_blog_topic,
    StepKind.article_refresh.value: _do_article_refresh,
    StepKind.product_seo.value: _do_product_seo,
}


def execute(step_id: int) -> StrategyStep:
    """Carry out one executable step, recording what happened either way.

    A failure is written to the row rather than raised past the caller: the
    owner clicked a button on a page, and the useful outcome is the page
    saying why it didn't work — not a traceback.
    """
    with get_session() as session:
        step = session.get(StrategyStep, step_id)
        if step is None:
            raise KeyError(f"no step {step_id}")
        if step.status == "done":
            session.expunge(step)
            return step
        kind, payload = step.kind, step.payload

    runner = _RUNNERS.get(kind)
    if runner is None:
        raise StepError(
            f"'{kind}' steps are tracked, not automated — this app has no "
            "write access for them. Mark it done once you've made the change."
        )

    try:
        result = runner(payload)
        status = "done"
    except Exception as exc:  # noqa: BLE001 - reported on the row
        log.exception("strategy step %s failed", step_id)
        result = f"{type(exc).__name__}: {exc}"
        status = "failed"

    with get_session() as session:
        step = session.get(StrategyStep, step_id)
        step.status = status
        step.result = result[:2000]
        step.done_at = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            if status == "done" else None
        )
        session.flush()
        session.expunge(step)
    return step


def set_step_status(step_id: int, status: str, note: str | None = None) -> None:
    """Mark a tracked step done or skipped by hand."""
    if status not in ("proposed", "done", "skipped"):
        raise ValueError(f"bad status {status!r}")
    with get_session() as session:
        step = session.get(StrategyStep, step_id)
        if step is None:
            raise KeyError(f"no step {step_id}")
        step.status = status
        if note:
            step.result = note[:2000]
        step.done_at = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            if status == "done" else None
        )
