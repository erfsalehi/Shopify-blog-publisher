"""Experiment scoring and the Shopify SEO write.

The scoring tests are mostly about *refusing* to produce a verdict. It is easy
to write a function that always returns a number and a p-value; the value here
is that it declines when the data can't support one, and that the baseline
can't drift under a running test.

The SEO tests cover two Shopify behaviours that fail silently — the mutation
reports success and stores something else — so an unverified write would
report an applied treatment that was never applied.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from dashboard import experiments
from dashboard.db import get_session
from dashboard.models import (
    Experiment,
    ExperimentProduct,
    GscPageDaily,
    ShopifyProduct,
)
from dashboard.product_seo import SeoWriteError, write_seo

TODAY = date(2026, 8, 5)
SETTLED = TODAY - timedelta(days=3)


def _product(n: int) -> ShopifyProduct:
    return ShopifyProduct(
        product_gid=f"gid://shopify/Product/{n}",
        handle=f"p{n}", title=f"Product {n}",
        online_url=f"https://drflooring.ca/products/p{n}",
    )


def _make_experiment(variable="title", **kwargs) -> int:
    with get_session() as session:
        row = Experiment(name=kwargs.pop("name", "exp"), variable=variable, **kwargs)
        session.add(row)
        session.flush()
        return row.id


def _seed(treatment: int, control: int, *, before: int, after_t: int, after_c: int):
    """Products with clicks per day before and after the start date.

    Impressions are held constant so a CTR change comes only from clicks.
    """
    start = SETTLED - timedelta(days=13)
    with get_session() as session:
        n = 0
        gids = {"treatment": [], "control": []}
        for cohort, count, after in (
            ("treatment", treatment, after_t), ("control", control, after_c)
        ):
            for _ in range(count):
                n += 1
                session.add(_product(n))
                gids[cohort].append(f"gid://shopify/Product/{n}")
                for day_offset in range(14):
                    day = start + timedelta(days=day_offset)
                    clicks = before if day_offset < 7 else after
                    session.add(GscPageDaily(
                        date=day, page=f"https://drflooring.ca/products/p{n}",
                        clicks=clicks, impressions=100, ctr=clicks / 100,
                        position=10.0,
                    ))
    return gids


def _enrol(experiment_id: int, gids: dict):
    with get_session() as session:
        for cohort, values in gids.items():
            for gid in values:
                session.add(ExperimentProduct(
                    experiment_id=experiment_id, product_gid=gid, cohort=cohort
                ))


# ── Refusing to score ──────────────────────────────────────────────


def test_a_draft_experiment_is_not_scored(dashboard_db):
    experiment_id = _make_experiment()
    result = experiments.score(experiment_id, today=TODAY)
    assert result["scorable"] is False
    assert "Not started" in result["reason"]


def test_too_few_products_gets_no_verdict(dashboard_db):
    """A difference measured across three products is a story about three
    products. Returning a p-value for it would be the harmful thing."""
    gids = _seed(3, 3, before=5, after_t=9, after_c=5)
    experiment_id = _make_experiment()
    _enrol(experiment_id, gids)
    experiments.freeze_baseline(experiment_id, today=SETTLED - timedelta(days=6))
    result = experiments.score(experiment_id, today=TODAY)
    assert result["scorable"] is False
    assert str(experiments.MIN_GROUP) in result["reason"]
    assert "p_value" not in result


def test_products_with_no_impressions_cannot_carry_a_result(dashboard_db):
    """Enough rows to pass the count check, but nothing to measure."""
    experiment_id = _make_experiment()
    with get_session() as session:
        for n in range(1, 13):
            session.add(_product(n))
    _enrol(experiment_id, {
        "treatment": [f"gid://shopify/Product/{n}" for n in range(1, 7)],
        "control": [f"gid://shopify/Product/{n}" for n in range(7, 13)],
    })
    experiments.freeze_baseline(experiment_id, today=SETTLED - timedelta(days=6))
    result = experiments.score(experiment_id, today=TODAY)
    assert result["scorable"] is False
    assert "no search impressions" in result["reason"]


# ── Difference-in-differences ──────────────────────────────────────


def test_a_site_wide_lift_is_not_credited_to_the_treatment(dashboard_db):
    """Both groups doubled, so the change did nothing. A before/after
    comparison would have reported a triumph."""
    gids = _seed(10, 20, before=5, after_t=10, after_c=10)
    experiment_id = _make_experiment()
    _enrol(experiment_id, gids)
    experiments.freeze_baseline(experiment_id, today=SETTLED - timedelta(days=6))
    result = experiments.score(experiment_id, today=TODAY)
    assert result["scorable"] is True
    assert result["difference"] == pytest.approx(0.0, abs=0.01)
    assert result["significant"] is False


def test_a_real_lift_above_the_control_is_detected(dashboard_db):
    gids = _seed(10, 20, before=5, after_t=12, after_c=6)
    experiment_id = _make_experiment()
    _enrol(experiment_id, gids)
    experiments.freeze_baseline(experiment_id, today=SETTLED - timedelta(days=6))
    result = experiments.score(experiment_id, today=TODAY)
    assert result["scorable"] is True
    assert result["difference"] > 0
    assert result["significant"] is True


def test_a_site_wide_decline_shows_the_treatment_held_up(dashboard_db):
    """Everything fell, but treatment fell less. Before/after would call that
    a failure; difference-in-differences calls it a win."""
    gids = _seed(10, 20, before=10, after_t=9, after_c=5)
    experiment_id = _make_experiment()
    _enrol(experiment_id, gids)
    experiments.freeze_baseline(experiment_id, today=SETTLED - timedelta(days=6))
    result = experiments.score(experiment_id, today=TODAY)
    assert result["treatment_delta"] < 0     # it did decline
    assert result["difference"] > 0          # ...by less than the control
    assert result["significant"] is True


def test_the_verdict_says_plainly_when_nothing_was_detected(dashboard_db):
    gids = _seed(10, 20, before=5, after_t=10, after_c=10)
    experiment_id = _make_experiment()
    _enrol(experiment_id, gids)
    experiments.freeze_baseline(experiment_id, today=SETTLED - timedelta(days=6))
    result = experiments.score(experiment_id, today=TODAY)
    assert "No detectable effect" in result["verdict"]
    assert "not a finding" in result["verdict"]


def test_scoring_is_deterministic(dashboard_db):
    """The permutation test is seeded: the same data must not produce a
    different p-value on refresh, or nobody can trust the page."""
    gids = _seed(10, 20, before=5, after_t=12, after_c=6)
    experiment_id = _make_experiment()
    _enrol(experiment_id, gids)
    experiments.freeze_baseline(experiment_id, today=SETTLED - timedelta(days=6))
    first = experiments.score(experiment_id, today=TODAY)
    second = experiments.score(experiment_id, today=TODAY)
    assert first["p_value"] == second["p_value"]


# ── Baseline integrity ─────────────────────────────────────────────


def test_the_baseline_is_frozen_not_recomputed(dashboard_db):
    """A baseline derived live would move as Search Console's windows moved,
    letting a test quietly produce whichever answer was hoped for."""
    gids = _seed(10, 20, before=5, after_t=12, after_c=6)
    experiment_id = _make_experiment()
    _enrol(experiment_id, gids)
    experiments.freeze_baseline(experiment_id, today=SETTLED - timedelta(days=6))
    with get_session() as session:
        before = [
            m.baseline_clicks for m in session.query(ExperimentProduct).all()
        ]
    # More history arrives; the frozen baseline must not budge.
    with get_session() as session:
        session.add(GscPageDaily(
            date=SETTLED - timedelta(days=20),
            page="https://drflooring.ca/products/p1",
            clicks=999, impressions=999, ctr=1.0, position=1.0,
        ))
    experiments.score(experiment_id, today=TODAY)
    with get_session() as session:
        after = [m.baseline_clicks for m in session.query(ExperimentProduct).all()]
    assert before == after


def test_membership_cannot_change_once_started(dashboard_db):
    """A treatment product that leaves the group invalidates the result
    rather than changing it."""
    gids = _seed(6, 6, before=5, after_t=9, after_c=5)
    experiment_id = _make_experiment()
    _enrol(experiment_id, gids)
    experiments.freeze_baseline(experiment_id, today=SETTLED - timedelta(days=6))
    with pytest.raises(ValueError, match="already running"):
        experiments.freeze_baseline(experiment_id, today=TODAY)


def test_starting_with_no_products_is_refused(dashboard_db):
    experiment_id = _make_experiment()
    with pytest.raises(ValueError, match="Add products"):
        experiments.freeze_baseline(experiment_id, today=TODAY)


# ── Control matching ───────────────────────────────────────────────


def test_proposed_controls_exclude_products_with_no_impressions(dashboard_db):
    """A product nobody sees can't show a seasonal dip, so it would dilute the
    control delta toward zero and turn this back into a before/after."""
    with get_session() as session:
        for n in range(1, 6):
            session.add(_product(n))
        # Only p1 and p2 have any traffic.
        for n in (1, 2):
            session.add(GscPageDaily(
                date=SETTLED, page=f"https://drflooring.ca/products/p{n}",
                clicks=5, impressions=100, ctr=0.05, position=8.0,
            ))
    proposed = experiments.propose_controls(
        ["gid://shopify/Product/1"], count=10, today=TODAY
    )
    handles = {c["product"].handle for c in proposed}
    assert handles == {"p2"}


def test_proposed_controls_never_include_the_treatment_itself(dashboard_db):
    with get_session() as session:
        for n in (1, 2):
            session.add(_product(n))
            session.add(GscPageDaily(
                date=SETTLED, page=f"https://drflooring.ca/products/p{n}",
                clicks=5, impressions=100, ctr=0.05, position=8.0,
            ))
    proposed = experiments.propose_controls(
        ["gid://shopify/Product/1"], count=10, today=TODAY
    )
    assert all(c["product"].handle != "p1" for c in proposed)


# ── The Shopify SEO write ──────────────────────────────────────────


class FakeShopify:
    def __init__(self, title="Sand Laminate-Stair Nose",
                 seo_title=None, seo_description="Existing description",
                 stores=True):
        self.product = {
            "id": "gid://shopify/Product/1", "title": title,
            "seo": {"title": seo_title, "description": seo_description},
        }
        self.stores = stores
        self.sent: list[dict] = []

    def graphql(self, query, variables=None):
        if "query ProductSeo" in query:
            return {"product": self.product}
        payload = variables["input"]["seo"]
        self.sent.append(payload)
        stored = dict(payload) if self.stores else {
            "title": None, "description": payload["description"]
        }
        return {"productUpdate": {
            "product": {**self.product, "seo": stored}, "userErrors": [],
        }}


def test_the_seo_description_is_preserved_when_only_the_title_changes(dashboard_db):
    """Shopify replaces the whole seo object rather than merging, so sending
    just the title silently erases the description."""
    client = FakeShopify(seo_description="Keep me")
    write_seo(client, "gid://shopify/Product/1", seo_title="New title")
    assert client.sent[0]["description"] == "Keep me"
    assert client.sent[0]["title"] == "New title"


def test_the_seo_title_is_preserved_when_only_the_description_changes(dashboard_db):
    client = FakeShopify(seo_title="Keep this title")
    write_seo(client, "gid://shopify/Product/1", seo_description="New description")
    assert client.sent[0]["title"] == "Keep this title"


def test_an_seo_title_equal_to_the_product_title_is_rejected(dashboard_db):
    """Shopify stores null for it and reports no error — for a title
    experiment that's the entire treatment silently not being applied."""
    client = FakeShopify(title="Sand Laminate-Stair Nose")
    with pytest.raises(SeoWriteError, match="silently discards"):
        write_seo(
            client, "gid://shopify/Product/1",
            seo_title="Sand Laminate-Stair Nose",
        )
    assert client.sent == []


def test_a_silently_rejected_write_is_caught_by_the_echo_check(dashboard_db):
    """Both traps present as success with the wrong value stored, so the
    response is verified rather than trusted."""
    client = FakeShopify(stores=False)
    with pytest.raises(SeoWriteError, match="silent rejection"):
        write_seo(client, "gid://shopify/Product/1", seo_title="New title")


def test_apply_treatment_records_before_and_after(dashboard_db):
    experiment_id = _make_experiment(variable="title")
    with get_session() as session:
        session.add(_product(1))
        session.add(ExperimentProduct(
            experiment_id=experiment_id,
            product_gid="gid://shopify/Product/1", cohort="treatment",
        ))
    client = FakeShopify(seo_title="Old SEO title")
    summary = experiments.apply_treatment(
        experiment_id, {"gid://shopify/Product/1": "New SEO title"}, client=client
    )
    assert summary["applied"] == 1
    with get_session() as session:
        row = session.query(ExperimentProduct).one()
    assert row.before_value == "Old SEO title"
    assert row.after_value == "New SEO title"
    assert row.applied_at is not None


def test_one_failure_does_not_abort_the_rest_of_the_cohort(dashboard_db):
    """Half a cohort applied with no record of which half is the worst
    possible state for an experiment."""
    experiment_id = _make_experiment(variable="title")
    with get_session() as session:
        for n in (1, 2):
            session.add(_product(n))
            session.add(ExperimentProduct(
                experiment_id=experiment_id,
                product_gid=f"gid://shopify/Product/{n}", cohort="treatment",
            ))

    class Flaky(FakeShopify):
        def graphql(self, query, variables=None):
            if variables and variables.get("input", {}).get("id", "").endswith("1"):
                raise RuntimeError("Shopify said no")
            return super().graphql(query, variables)

    summary = experiments.apply_treatment(
        experiment_id,
        {"gid://shopify/Product/1": "A", "gid://shopify/Product/2": "B"},
        client=Flaky(),
    )
    assert summary["applied"] == 1
    assert summary["failed"] == 1
    with get_session() as session:
        errored = session.query(ExperimentProduct).filter(
            ExperimentProduct.apply_error.isnot(None)
        ).count()
    assert errored == 1


def test_a_price_experiment_refuses_to_write_to_shopify(dashboard_db):
    """Price changes are made by hand in admin — this app should not be
    driving a store's price field."""
    experiment_id = _make_experiment(variable="price", name="price-test")
    with pytest.raises(ValueError, match="applied by hand"):
        experiments.apply_treatment(experiment_id, {"gid://x": "10.00"})
