"""Cohort experiments: matching, baselines, and difference-in-differences.

Start with what this cannot do, because the design is shaped by it.

**Per-visitor split testing is impossible here.** Google shows one title to
everyone, and Shopify cannot show different prices to different visitors. Any
tool claiming an A/B test on this stack is measuring something else. What is
possible, and is what SEO practitioners actually do, is a treatment group
against a matched control group over the same dates.

**Difference-in-differences** is the scoring method: Δtreatment − Δcontrol.
The control group absorbs everything that happened to the whole site in the
window — a seasonal dip, an algorithm update, a competitor's campaign — so
what's left is attributable to the change. Comparing treatment before-and-
after alone would credit the change with the season.

**Significance comes from a permutation test**, not a t-test. With 10
products against 50 there is no reason to believe per-product deltas are
normally distributed, and a t-test's p-value would be a number with more
confidence than the data supports. Shuffling the group labels a few thousand
times and asking how often chance produces an effect this large assumes
nothing, needs no scipy, and is easy to explain to the person reading it.

And when there isn't enough data, `score` says so instead of returning a
number. A verdict from four products is not a small result, it is not a
result.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func

from dashboard.db import get_session
from dashboard.jobs.gsc import settled_through
from dashboard.models import (
    Experiment,
    ExperimentProduct,
    GscPageDaily,
    ShopifyProduct,
)
from dashboard.reporting import Metrics, normalize_url

log = logging.getLogger(__name__)

VARIABLES = ("title", "description", "price", "strategy")

# Below this many products in *either* group, no verdict is offered. Not a
# statistical threshold so much as a floor of decency: a difference measured
# across three products is a story about three products.
MIN_GROUP = 5

# Enough shuffles for a stable p-value at the resolution anyone will act on.
PERMUTATIONS = 5000


@dataclass(frozen=True)
class GroupResult:
    cohort: str
    products: int
    before: Metrics
    after: Metrics

    @property
    def ctr_delta(self) -> float:
        return (self.after.ctr - self.before.ctr) * 100

    @property
    def clicks_delta(self) -> int:
        return self.after.clicks - self.before.clicks

    @property
    def impressions_delta(self) -> int:
        return self.after.impressions - self.before.impressions

    @property
    def position_delta(self) -> float:
        return self.after.position - self.before.position


def _metric_for(variable: str) -> str:
    """Which number answers the question this experiment asked.

    A title test changes what searchers see, not where the page ranks, so its
    metric is click-through rate. A price test is about whether a visible
    number makes people act, so it's clicks against the impressions that were
    already there.
    """
    return {
        "title": "ctr",
        "description": "ctr",
        "price": "clicks",
        "strategy": "clicks",
    }.get(variable, "ctr")


def _window_metrics(
    session, gids: list[str], start: date, end: date
) -> dict[str, Metrics]:
    """Per-product search metrics over a window, keyed by product gid."""
    if not gids:
        return {}
    products = (
        session.query(ShopifyProduct)
        .filter(ShopifyProduct.product_gid.in_(gids))
        .all()
    )
    by_url = {normalize_url(p.online_url): p.product_gid for p in products if p.online_url}
    if not by_url:
        return {}

    out: dict[str, Metrics] = {}
    rows = (
        session.query(
            GscPageDaily.page,
            func.coalesce(func.sum(GscPageDaily.clicks), 0),
            func.coalesce(func.sum(GscPageDaily.impressions), 0),
            func.coalesce(
                func.sum(GscPageDaily.position * GscPageDaily.impressions), 0.0
            ),
        )
        .filter(GscPageDaily.date >= start, GscPageDaily.date <= end)
        .group_by(GscPageDaily.page)
        .all()
    )
    for page, clicks, impressions, weight in rows:
        gid = by_url.get(normalize_url(page))
        if gid is None:
            continue
        prior = out.get(gid, Metrics())
        out[gid] = Metrics(
            prior.clicks + int(clicks),
            prior.impressions + int(impressions),
            prior.position_weight + float(weight),
        )
    return out


def propose_controls(
    treatment_gids: list[str], *, count: int = 50, window_days: int = 28,
    today: date | None = None,
) -> list[dict]:
    """Candidate controls, matched on impressions.

    Matching on impressions rather than picking at random is what makes the
    control group absorb the same seasonality as the treatment group: a
    product nobody sees cannot show a seasonal dip, so a control set of
    invisible products silently turns the comparison back into a
    before-and-after.

    Returned for the owner to confirm, never auto-committed — PLAN.md is
    explicit that a human approves the set, and cross-catalogue matching is
    the kind of thing that looks right and isn't.
    """
    today = today or date.today()
    end = settled_through(today)
    start = end - timedelta(days=window_days - 1)

    with get_session() as session:
        all_products = session.query(ShopifyProduct).all()
        for p in all_products:
            session.expunge(p)
        gids = [p.product_gid for p in all_products]
        metrics = _window_metrics(session, gids, start, end)

    treatment = set(treatment_gids)
    targets = [
        metrics.get(gid, Metrics()).impressions
        for gid in treatment_gids
    ]
    if not targets:
        return []
    target_mean = sum(targets) / len(targets)

    pool = [
        p for p in all_products
        if p.product_gid not in treatment
        and metrics.get(p.product_gid, Metrics()).impressions > 0
    ]
    # Closest on impressions first. A control with no impressions at all can't
    # move, so it would dilute the control delta toward zero.
    pool.sort(
        key=lambda p: abs(
            metrics.get(p.product_gid, Metrics()).impressions - target_mean
        )
    )
    return [
        {
            "product": p,
            "impressions": metrics.get(p.product_gid, Metrics()).impressions,
            "clicks": metrics.get(p.product_gid, Metrics()).clicks,
        }
        for p in pool[:count]
    ]


def freeze_baseline(experiment_id: int, today: date | None = None) -> dict:
    """Start the experiment: snapshot every member's pre-period metrics.

    Written once and never recomputed. If "before" were derived from live data
    at scoring time, the baseline would drift as Search Console's windows
    moved and the experiment could quietly produce whichever answer was hoped
    for.
    """
    today = today or date.today()
    with get_session() as session:
        experiment = session.get(Experiment, experiment_id)
        if experiment is None:
            raise ValueError(f"No experiment {experiment_id}")
        if experiment.status != "draft":
            raise ValueError(
                f"Experiment is already {experiment.status}; its baseline is "
                "frozen and membership can't change."
            )
        members = session.query(ExperimentProduct).filter(
            ExperimentProduct.experiment_id == experiment_id
        ).all()
        if not members:
            raise ValueError("Add products to both cohorts before starting.")

        end = settled_through(today)
        start = end - timedelta(days=experiment.baseline_days - 1)
        metrics = _window_metrics(
            session, [m.product_gid for m in members], start, end
        )
        now = datetime.now(timezone.utc)
        for member in members:
            found = metrics.get(member.product_gid, Metrics())
            member.baseline_clicks = found.clicks
            member.baseline_impressions = found.impressions
            member.baseline_position_weight = found.position_weight
            member.baseline_frozen_at = now

        experiment.status = "running"
        experiment.started_on = today
        counts = {"treatment": 0, "control": 0}
        for member in members:
            counts[member.cohort] = counts.get(member.cohort, 0) + 1

    return {
        "baseline_window": (start, end),
        "treatment": counts.get("treatment", 0),
        "control": counts.get("control", 0),
    }


def _aggregate(members, key) -> Metrics:
    total = Metrics()
    for member in members:
        found = key(member)
        total = Metrics(
            total.clicks + found.clicks,
            total.impressions + found.impressions,
            total.position_weight + found.position_weight,
        )
    return total


def _per_product_deltas(members, after: dict[str, Metrics], metric: str) -> list[float]:
    """One delta per product — the unit the permutation test shuffles."""
    deltas = []
    for member in members:
        before = Metrics(
            member.baseline_clicks,
            member.baseline_impressions,
            member.baseline_position_weight,
        )
        now = after.get(member.product_gid, Metrics())
        if metric == "ctr":
            # A product with no impressions either side has no CTR to move.
            if not before.impressions and not now.impressions:
                continue
            deltas.append((now.ctr - before.ctr) * 100)
        else:
            deltas.append(float(now.clicks - before.clicks))
    return deltas


def _permutation_p(
    treatment: list[float], control: list[float], observed: float
) -> float:
    """How often chance alone produces an effect at least this large.

    Two-sided, and assumes nothing about the distribution — which is the
    point. With 10 products against 50, a t-test's p-value would carry more
    confidence than the data can support.
    """
    pooled = treatment + control
    if len(treatment) < 2 or len(control) < 2:
        return 1.0
    rng = random.Random(20260805)  # fixed: the same data must score the same
    n_treatment = len(treatment)
    at_least_as_extreme = 0
    for _ in range(PERMUTATIONS):
        rng.shuffle(pooled)
        left = pooled[:n_treatment]
        right = pooled[n_treatment:]
        effect = (sum(left) / len(left)) - (sum(right) / len(right))
        if abs(effect) >= abs(observed):
            at_least_as_extreme += 1
    return at_least_as_extreme / PERMUTATIONS


def score(experiment_id: int, today: date | None = None) -> dict:
    """Difference-in-differences, with an honest verdict or none at all."""
    today = today or date.today()
    with get_session() as session:
        experiment = session.get(Experiment, experiment_id)
        if experiment is None:
            raise ValueError(f"No experiment {experiment_id}")
        session.expunge(experiment)
        members = session.query(ExperimentProduct).filter(
            ExperimentProduct.experiment_id == experiment_id
        ).all()
        for m in members:
            session.expunge(m)

    if experiment.status == "draft":
        return {
            "experiment": experiment,
            "scorable": False,
            "reason": "Not started yet — no baseline has been frozen.",
        }

    end = settled_through(today)
    start = experiment.started_on or (end - timedelta(days=experiment.baseline_days))
    # The test window runs from the start date to the last settled day.
    if start > end:
        return {
            "experiment": experiment,
            "scorable": False,
            "reason": (
                f"Started {start}; there is no settled data after it yet. "
                f"Search Console is settled through {end}."
            ),
        }

    with get_session() as session:
        after = _window_metrics(
            session, [m.product_gid for m in members], start, end
        )

    groups = {}
    for cohort in ("treatment", "control"):
        cohort_members = [m for m in members if m.cohort == cohort]
        groups[cohort] = GroupResult(
            cohort=cohort,
            products=len(cohort_members),
            before=_aggregate(cohort_members, lambda m: Metrics(
                m.baseline_clicks, m.baseline_impressions,
                m.baseline_position_weight,
            )),
            after=_aggregate(
                cohort_members, lambda m: after.get(m.product_gid, Metrics())
            ),
        )

    metric = _metric_for(experiment.variable)
    treatment_members = [m for m in members if m.cohort == "treatment"]
    control_members = [m for m in members if m.cohort == "control"]

    if len(treatment_members) < MIN_GROUP or len(control_members) < MIN_GROUP:
        return {
            "experiment": experiment,
            "groups": groups,
            "metric": metric,
            "test_window": (start, end),
            "scorable": False,
            "reason": (
                f"Needs at least {MIN_GROUP} products in each group "
                f"(treatment has {len(treatment_members)}, control has "
                f"{len(control_members)}). A difference measured across fewer "
                "than that is a story about those products, not a result."
            ),
        }

    treatment_deltas = _per_product_deltas(treatment_members, after, metric)
    control_deltas = _per_product_deltas(control_members, after, metric)
    if len(treatment_deltas) < MIN_GROUP or len(control_deltas) < MIN_GROUP:
        return {
            "experiment": experiment,
            "groups": groups,
            "metric": metric,
            "test_window": (start, end),
            "scorable": False,
            "reason": (
                "Too many products have no search impressions in either "
                "window to compare. Products nobody sees cannot show an "
                "effect either way."
            ),
        }

    treatment_mean = sum(treatment_deltas) / len(treatment_deltas)
    control_mean = sum(control_deltas) / len(control_deltas)
    did = treatment_mean - control_mean
    p_value = _permutation_p(treatment_deltas, control_deltas, did)

    days_running = (end - start).days + 1
    return {
        "experiment": experiment,
        "groups": groups,
        "metric": metric,
        "test_window": (start, end),
        "days_running": days_running,
        "scorable": True,
        "treatment_delta": treatment_mean,
        "control_delta": control_mean,
        "difference": did,
        "p_value": p_value,
        "significant": p_value < 0.05,
        # Stated separately from significance on purpose: a result can clear
        # p<0.05 on three weeks of data and still be an artefact of the window.
        "young": days_running < 28,
        "verdict": _verdict(did, p_value, metric, days_running),
    }


def apply_treatment(
    experiment_id: int, values: dict[str, str], *, client=None
) -> dict:
    """Write the treatment to Shopify for each treatment product.

    `values` maps product gid → the new SEO title or description, depending on
    the experiment's variable. Only `title` and `description` are written:
    price changes are made by hand in admin (Shopify's price field is not
    something this app should be driving), and the app records the event.

    Each product is attempted independently. One failure must not leave half
    the cohort in an unknown state with no record of which half.
    """
    from blog_pipeline.tools.shopify import ShopifyClient

    from dashboard.product_seo import write_seo

    with get_session() as session:
        experiment = session.get(Experiment, experiment_id)
        if experiment is None:
            raise ValueError(f"No experiment {experiment_id}")
        variable = experiment.variable
        members = session.query(ExperimentProduct).filter(
            ExperimentProduct.experiment_id == experiment_id,
            ExperimentProduct.cohort == "treatment",
        ).all()
        for m in members:
            session.expunge(m)

    if variable not in ("title", "description"):
        raise ValueError(
            f"'{variable}' experiments are applied by hand — this app only "
            "writes SEO title and description."
        )

    close_after = client is None
    client = client or ShopifyClient()
    applied = failed = 0
    results = []
    try:
        for member in members:
            new_value = values.get(member.product_gid)
            if not new_value or not new_value.strip():
                continue
            field = "seo_title" if variable == "title" else "seo_description"
            try:
                outcome = write_seo(client, member.product_gid, **{field: new_value})
                before = outcome["before"][field]
                with get_session() as session:
                    row = session.get(ExperimentProduct, member.id)
                    row.before_value = before
                    row.after_value = new_value
                    row.applied_at = datetime.now(timezone.utc)
                    row.apply_error = None
                applied += 1
                results.append({
                    "product_gid": member.product_gid, "ok": True,
                    "before": before, "after": new_value,
                })
            except Exception as exc:  # noqa: BLE001
                failed += 1
                with get_session() as session:
                    row = session.get(ExperimentProduct, member.id)
                    row.apply_error = str(exc)[:800]
                results.append({
                    "product_gid": member.product_gid, "ok": False,
                    "error": str(exc),
                })
                log.warning("treatment failed for %s: %s", member.product_gid, exc)
    finally:
        if close_after:
            client.close()

    return {"applied": applied, "failed": failed, "results": results}


def _verdict(did: float, p_value: float, metric: str, days: int) -> str:
    unit = "percentage points of CTR" if metric == "ctr" else "clicks per product"
    direction = "better" if did > 0 else "worse"
    magnitude = f"{abs(did):.2f} {unit}"
    if p_value >= 0.05:
        return (
            f"No detectable effect. The treatment group did {magnitude} "
            f"{direction} than the control, but chance alone produces a "
            f"difference at least that large {p_value * 100:.0f}% of the time "
            "— which is not a finding."
        )
    caveat = (
        f" Only {days} days of data so far, so treat this as provisional."
        if days < 28 else ""
    )
    return (
        f"The treatment group did {magnitude} {direction} than the control. "
        f"Chance produces a difference this large {p_value * 100:.1f}% of the "
        f"time.{caveat}"
    )
