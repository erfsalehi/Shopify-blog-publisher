"""Turning advice into a button, and refusing to when it isn't earned.

The claim this feature makes beyond "the advisor emits JSON" is that **a
button is a promise**: if one is rendered, pressing it cannot fail for a
reason the app already knew. So most of these tests are about the downgrade
path — the ways a suggestion loses its button and keeps its text.

The rest are about ordering. `make_experiment` must not write to Shopify,
because an experiment whose treatment is applied before its baseline is frozen
measures the change against itself.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from dashboard.jobs.gsc import settled_through

import pytest

from dashboard import advisor
from dashboard.db import get_session
from dashboard.models import (
    Experiment,
    ExperimentProduct,
    GscPageDaily,
    ShopifyProduct,
)

BRIEF = "CATALOGUE: 900 products\nproduct | handle | clicks\nOak | oak-12mm | 4\n"


def _catalogue(*handles: str) -> None:
    with get_session() as session:
        for n, handle in enumerate(handles, start=1):
            session.add(ShopifyProduct(
                product_gid=f"gid://shopify/Product/{n}",
                handle=handle, title=f"Product {handle}",
                online_url=f"https://drflooring.ca/products/{handle}",
            ))


def _generate(monkeypatch, actions: list, scope: str = "products"):
    reply = json.dumps({"summary": "s", "reading": "r", "actions": actions})
    monkeypatch.setattr(
        advisor, "_call",
        lambda *a, **k: (reply, {"input": 1, "output": 1}, "fake"),
    )
    monkeypatch.setattr(
        "dashboard.advisor_context.build_context", lambda s, today=None: BRIEF
    )
    advisor.generate(scope)
    return advisor.actions_for(scope)


PRODUCT_ACTION = {
    "text": "Lead these titles with the price",
    "kind": "product_seo",
    "products": [
        {"handle": "oak-12mm", "seo_title": "Oak 12mm - $4.29/sqft | D&R"},
    ],
}


# ── The downgrade path ─────────────────────────────────────────────


def test_a_handle_that_does_not_exist_loses_the_button_not_the_advice(
    dashboard_db, monkeypatch
):
    """The dangerous case. A confident payload naming a product that isn't in
    the catalogue must never reach a button — but the advice may still be
    right, so the text survives."""
    _catalogue("oak-12mm")
    invented = dict(PRODUCT_ACTION, products=[
        {"handle": "a-product-we-do-not-sell", "seo_title": "Anything"}
    ])
    (action,) = _generate(monkeypatch, [invented])
    assert action.text == PRODUCT_ACTION["text"]
    assert action.kind == "manual"
    assert action.executable is False
    assert action.products == []


def test_only_the_handles_that_resolve_survive(dashboard_db, monkeypatch):
    """A partly-real payload keeps its real half rather than being thrown out
    whole — the two products that exist are still worth one click."""
    _catalogue("oak-12mm", "maple-8mm")
    mixed = dict(PRODUCT_ACTION, products=[
        {"handle": "oak-12mm", "seo_title": "Oak"},
        {"handle": "ghost", "seo_title": "Ghost"},
        {"handle": "maple-8mm", "seo_title": "Maple"},
    ])
    (action,) = _generate(monkeypatch, [mixed])
    assert action.executable is True
    assert {p["handle"] for p in action.products} == {"oak-12mm", "maple-8mm"}


def test_a_product_with_nothing_to_write_is_dropped(dashboard_db, monkeypatch):
    """Neither a title nor a description is not a write, and a button that
    calls productUpdate with no fields is a button that does nothing."""
    _catalogue("oak-12mm")
    empty = dict(PRODUCT_ACTION, products=[{"handle": "oak-12mm"}])
    (action,) = _generate(monkeypatch, [empty])
    assert action.kind == "manual"


def test_an_absurdly_long_title_is_refused(dashboard_db, monkeypatch):
    """Shopify truncates rather than rejecting, so nothing downstream would
    catch this — a 400-character 'title' is a model that misread the field."""
    _catalogue("oak-12mm")
    long = dict(PRODUCT_ACTION, products=[
        {"handle": "oak-12mm", "seo_title": "x" * (advisor.MAX_SEO_TITLE + 1)}
    ])
    (action,) = _generate(monkeypatch, [long])
    assert action.kind == "manual"


def test_a_plain_string_action_still_works(dashboard_db, monkeypatch):
    """Rule 6 is a request, not a schema. The model still returns strings and
    every action stored before this feature existed is one."""
    (action,) = _generate(monkeypatch, ["Phone the three biggest fallers"])
    assert action.text == "Phone the three biggest fallers"
    assert action.kind == "manual"


def test_an_unknown_kind_becomes_manual(dashboard_db, monkeypatch):
    _catalogue("oak-12mm")
    weird = dict(PRODUCT_ACTION, kind="delete_everything")
    (action,) = _generate(monkeypatch, [weird])
    assert action.kind == "manual"


def test_a_valid_payload_is_resolved_to_real_rows(dashboard_db, monkeypatch):
    """Resolution happens once, at generation time: the gid is pinned so a
    product later re-handled in Shopify can't redirect the write."""
    _catalogue("oak-12mm")
    (action,) = _generate(monkeypatch, [PRODUCT_ACTION])
    assert action.executable is True
    (item,) = action.products
    assert item["product_gid"] == "gid://shopify/Product/1"
    assert item["title"] == "Product oak-12mm"
    assert item["seo_title"] == "Oak 12mm - $4.29/sqft | D&R"


def test_the_grounding_check_still_reads_action_text(dashboard_db, monkeypatch):
    """Typed actions must not smuggle an unchecked figure past
    `unverified_figures` — the text is still what gets scanned."""
    _catalogue("oak-12mm")
    invented = dict(PRODUCT_ACTION, text="These 8,412 pages need work")
    _generate(monkeypatch, [invented])
    assert "8,412" in advisor.latest_note("products").unverified


# ── Applying ───────────────────────────────────────────────────────


class _FakeClient:
    """Stands in for ShopifyClient. `write_seo` is patched over, so this only
    has to be closeable."""

    def close(self):
        pass


@pytest.fixture
def _no_shopify(monkeypatch):
    monkeypatch.setattr("blog_pipeline.tools.shopify.ShopifyClient", _FakeClient)


def _stub_write(monkeypatch, fn):
    monkeypatch.setattr("dashboard.product_seo.write_seo", fn)


def test_applying_writes_every_product_and_marks_the_action_done(
    dashboard_db, monkeypatch, _no_shopify
):
    _catalogue("oak-12mm", "maple-8mm")
    both = dict(PRODUCT_ACTION, products=[
        {"handle": "oak-12mm", "seo_title": "Oak"},
        {"handle": "maple-8mm", "seo_title": "Maple"},
    ])
    (action,) = _generate(monkeypatch, [both])

    written = []

    def record(client, gid, **kw):
        written.append((gid, kw))
        return {"before": {}, "after": kw}

    _stub_write(monkeypatch, record)
    result = advisor.run_action(action.id)

    assert len(written) == 2
    assert result.run_status == "done"
    assert result.status == "done"  # feeds the advisor's memory next run
    assert "Updated 2 of 2" in result.run_result


def test_one_failure_does_not_abandon_the_rest(
    dashboard_db, monkeypatch, _no_shopify
):
    """A half-written set with no record of which half is the outcome this is
    designed against."""
    _catalogue("oak-12mm", "maple-8mm")
    both = dict(PRODUCT_ACTION, products=[
        {"handle": "oak-12mm", "seo_title": "Oak"},
        {"handle": "maple-8mm", "seo_title": "Maple"},
    ])
    (action,) = _generate(monkeypatch, [both])

    def flaky(client, gid, **kw):
        if gid.endswith("/1"):
            raise RuntimeError("422 handle taken")
        return {"before": {}, "after": kw}

    _stub_write(monkeypatch, flaky)
    result = advisor.run_action(action.id)

    assert result.run_status == "failed"
    assert "422 handle taken" in result.run_result
    assert "Updated 1 of 2" in result.run_result
    # A partial write is not done — the owner still has one product to fix.
    assert result.status == "open"


def test_a_manual_suggestion_refuses_to_run(dashboard_db, monkeypatch):
    (action,) = _generate(monkeypatch, ["Phone your three biggest customers"])
    with pytest.raises(advisor.ActionError) as caught:
        advisor.run_action(action.id)
    assert "no write access" in str(caught.value)


def test_running_twice_does_not_write_twice(
    dashboard_db, monkeypatch, _no_shopify
):
    _catalogue("oak-12mm")
    (action,) = _generate(monkeypatch, [PRODUCT_ACTION])
    calls = []

    def record(client, gid, **kw):
        calls.append(gid)
        return {"before": {}, "after": kw}

    _stub_write(monkeypatch, record)
    advisor.run_action(action.id)
    advisor.run_action(action.id)
    assert len(calls) == 1


# ── Testing it instead ─────────────────────────────────────────────


def test_making_an_experiment_writes_nothing_to_shopify(
    dashboard_db, monkeypatch
):
    """The ordering the whole experiment design rests on. Applying before the
    baseline is frozen measures the change against itself."""
    _catalogue("oak-12mm")
    (action,) = _generate(monkeypatch, [PRODUCT_ACTION])

    def explode(*a, **k):
        raise AssertionError("make_experiment must not write to Shopify")

    monkeypatch.setattr("dashboard.product_seo.write_seo", explode)
    monkeypatch.setattr("blog_pipeline.tools.shopify.ShopifyClient", explode)

    experiment_id = advisor.make_experiment(action.id)
    with get_session() as session:
        assert session.get(Experiment, experiment_id).status == "draft"
        members = session.query(ExperimentProduct).filter(
            ExperimentProduct.experiment_id == experiment_id
        ).all()
        assert all(m.applied_at is None for m in members)


def test_the_suggested_value_rides_along_to_pre_fill_the_apply_form(
    dashboard_db, monkeypatch
):
    _catalogue("oak-12mm")
    (action,) = _generate(monkeypatch, [PRODUCT_ACTION])
    experiment_id = advisor.make_experiment(action.id)
    with get_session() as session:
        member = session.query(ExperimentProduct).filter(
            ExperimentProduct.experiment_id == experiment_id,
            ExperimentProduct.cohort == "treatment",
        ).one()
        assert member.proposed_value == "Oak 12mm - $4.29/sqft | D&R"
        # Distinct from after_value on purpose: nothing has been written.
        assert member.after_value is None


def test_controls_are_matched_and_added(dashboard_db, monkeypatch):
    """Without controls the difference-in-differences has nothing to subtract,
    so a one-click experiment that skipped them would quietly be a
    before-and-after."""
    _catalogue("oak-12mm", *[f"other-{n}" for n in range(8)])
    # Dated relative to today, not written out. `propose_controls` matches on
    # impressions in a 28-day window ending at the last settled day, so a
    # hard-coded date is a test that passes when written and fails weeks
    # later for a reason that has nothing to do with the code.
    seeded = settled_through(date.today()) - timedelta(days=5)
    with get_session() as session:
        for n in range(8):
            session.add(GscPageDaily(
                date=seeded,
                page=f"https://drflooring.ca/products/other-{n}",
                clicks=2, impressions=200, ctr=0.01, position=5.0,
            ))
    (action,) = _generate(monkeypatch, [PRODUCT_ACTION])
    experiment_id = advisor.make_experiment(action.id)
    with get_session() as session:
        controls = session.query(ExperimentProduct).filter(
            ExperimentProduct.experiment_id == experiment_id,
            ExperimentProduct.cohort == "control",
        ).count()
    assert controls > 0


def test_a_second_click_reuses_the_experiment(dashboard_db, monkeypatch):
    """Two clicks must not leave two half-built experiments and a name
    collision."""
    _catalogue("oak-12mm")
    (action,) = _generate(monkeypatch, [PRODUCT_ACTION])
    first = advisor.make_experiment(action.id)
    assert advisor.make_experiment(action.id) == first
    with get_session() as session:
        assert session.query(Experiment).count() == 1


def test_a_manual_suggestion_cannot_become_an_experiment(
    dashboard_db, monkeypatch
):
    (action,) = _generate(monkeypatch, ["Start a podcast"])
    with pytest.raises(advisor.ActionError):
        advisor.make_experiment(action.id)


def test_the_experiment_tests_the_field_the_suggestion_fills(
    dashboard_db, monkeypatch
):
    """A description rewrite scored as a title test would be scored on the
    wrong metric — `_metric_for` picks CTR for titles."""
    _catalogue("oak-12mm")
    desc_only = dict(PRODUCT_ACTION, products=[
        {"handle": "oak-12mm", "seo_description": "Oak flooring in Langley."}
    ])
    (action,) = _generate(monkeypatch, [desc_only])
    experiment_id = advisor.make_experiment(action.id)
    with get_session() as session:
        assert session.get(Experiment, experiment_id).variable == "description"


# ── The panel and the routes ───────────────────────────────────────
#
# Jinja errors are runtime-only: a template referencing a field that isn't
# there renders fine in every test that never loads the page. These two ask
# for the page.


@pytest.fixture
def client(dashboard_db):
    from fastapi.testclient import TestClient

    from dashboard.web import create_app

    with TestClient(create_app()) as c:
        yield c


def test_the_panel_shows_the_change_before_offering_the_button(
    client, monkeypatch
):
    """A button that writes to live products without first showing exactly
    what it will write is a button nobody should press."""
    _catalogue("oak-12mm")
    _generate(monkeypatch, [PRODUCT_ACTION])
    body = client.get("/products").text
    assert "Oak 12mm - $4.29/sqft | D&amp;R" in body   # the proposed value
    assert "/run" in body and "/experiment" in body    # both buttons


def test_a_manual_suggestion_gets_no_buttons(client, monkeypatch):
    _generate(monkeypatch, ["Start a podcast"])
    body = client.get("/products").text
    assert "Start a podcast" in body
    assert "/run" not in body


def test_the_run_route_writes_and_returns_to_the_page(
    client, monkeypatch, _no_shopify
):
    _catalogue("oak-12mm")
    (action,) = _generate(monkeypatch, [PRODUCT_ACTION])
    written = []

    def record(c, gid, **kw):
        written.append(gid)
        return {"before": {}, "after": kw}

    _stub_write(monkeypatch, record)
    response = client.post(
        f"/advisor/action/{action.id}/run",
        data={"back": "/products"}, follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/products"
    assert written == ["gid://shopify/Product/1"]


def test_the_experiment_route_lands_on_the_experiment(client, monkeypatch):
    _catalogue("oak-12mm")
    (action,) = _generate(monkeypatch, [PRODUCT_ACTION])
    response = client.post(
        f"/advisor/action/{action.id}/experiment", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/experiments/")
    # The draft page shows what would be written before asking the owner to
    # freeze a baseline — approving a change sight-unseen is the thing to
    # avoid. The apply form itself only appears once it's running.
    body = client.get(response.headers["location"]).text
    assert "Proposed treatment" in body
    assert "Oak 12mm - $4.29/sqft | D&amp;R" in body


def test_the_receipt_survives_the_row_moving_into_history(
    client, monkeypatch, _no_shopify
):
    """A successful Apply marks the suggestion done, which moves it straight
    out of the open list. If the result didn't follow it, pressing the button
    would make the row vanish with no record of what reached Shopify."""
    _catalogue("oak-12mm")
    (action,) = _generate(monkeypatch, [PRODUCT_ACTION])
    _stub_write(monkeypatch, lambda c, gid, **kw: {"before": {}, "after": kw})
    client.post(f"/advisor/action/{action.id}/run", data={"back": "/products"})

    body = client.get("/products").text
    assert "Updated 1 of 1" in body
