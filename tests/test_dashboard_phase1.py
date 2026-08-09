"""The Phase 1 syncs and the pages built on them.

Weighted toward the joins and the shapes, because those are where this layer
gets things quietly wrong: a URL join that matches nothing looks exactly like
a catalogue with no traffic, and a response unpacked in the wrong shape looks
exactly like a property with no data. Both happened during the build; both
have a test here now.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from dashboard import reporting, store
from dashboard.db import get_session
from dashboard.jobs.ads_windsor import WindsorClient, sync_ads_windsor
from dashboard.jobs.ga4 import sync_ga4_daily
from dashboard.jobs.shopify_catalog import sync_shopify_catalog
from dashboard.models import (
    AdsCampaignDaily,
    Ga4Daily,
    Ga4EventDaily,
    GscPageDaily,
    ExperimentProduct,
    ShopifyProduct,
)

TODAY = date(2026, 8, 5)
SETTLED = TODAY - timedelta(days=3)


def _stub_store(values: dict):
    real = store.get
    return lambda key: values.get(key, real(key))


# ── GA4 ────────────────────────────────────────────────────────────


class FakeGA4:
    """Mimics AnalyticsClient, including the shape it returns.

    That shape is the whole point: `run_report` has already flattened GA4's
    {dimensionValues: [{value}]} into plain lists. Re-flattening it produced
    empty rows, a successful call and a silent zero.
    """

    def __init__(self, *, enabled=True, event_names=("call_click", "whatsapp_click")):
        self.enabled = enabled
        self.property_id = "272416383"
        self._events = event_names

    def run_report(self, *, dimensions, metrics, start_date, end_date, limit=50000):
        rows = []
        day = start_date
        while day <= end_date:
            stamp = day.strftime("%Y%m%d")
            if dimensions == ["date"]:
                rows.append({"dimensions": [stamp], "metrics": ["100", "80", "60"]})
            else:
                for name in self._events:
                    rows.append({"dimensions": [stamp, name], "metrics": ["2"]})
            day += timedelta(days=1)
        return rows


def test_ga4_rows_are_read_in_the_clients_own_shape(dashboard_db, monkeypatch):
    """Regression: the first version looked for `dimensionValues` and got a
    clean 200 with zero rows stored — a bug that reads as 'no data'."""
    monkeypatch.setattr(store, "get", _stub_store({
        store.GA4_BACKFILL_DAYS: 4, store.GA4_RECENT_DAYS: 2,
        store.GA4_EVENTS: ["call_click", "whatsapp_click"],
    }))
    result = sync_ga4_daily(client=FakeGA4(), today=TODAY)
    assert result.rows > 0
    with get_session() as session:
        assert session.query(Ga4Daily).count() == 5
        assert session.query(Ga4EventDaily).count() == 10


def test_ga4_only_stores_configured_events(dashboard_db, monkeypatch):
    """click_phone_number duplicates call_click exactly in the live property.
    Storing every event GA4 returns would let a later sum double the calls."""
    monkeypatch.setattr(store, "get", _stub_store({
        store.GA4_BACKFILL_DAYS: 2, store.GA4_RECENT_DAYS: 1,
        store.GA4_EVENTS: ["call_click"],
    }))
    sync_ga4_daily(
        client=FakeGA4(event_names=("call_click", "click_phone_number")),
        today=TODAY,
    )
    with get_session() as session:
        stored = {n for (n,) in session.query(Ga4EventDaily.event_name).distinct()}
    assert stored == {"call_click"}


def test_a_configured_event_ga4_has_never_seen_is_reported(dashboard_db, monkeypatch):
    """Otherwise a typo'd event name presents as 'conversions: 0', which is
    indistinguishable from a genuinely bad month."""
    monkeypatch.setattr(store, "get", _stub_store({
        store.GA4_BACKFILL_DAYS: 2, store.GA4_RECENT_DAYS: 1,
        store.GA4_EVENTS: ["call_click", "call_for_price_click"],
    }))
    result = sync_ga4_daily(client=FakeGA4(event_names=("call_click",)), today=TODAY)
    assert result.detail["events_not_found_in_ga4"] == ["call_for_price_click"]


def test_unconfigured_ga4_is_skipped_not_failed(dashboard_db):
    result = sync_ga4_daily(client=FakeGA4(enabled=False), today=TODAY)
    assert result.skipped is True
    assert "NUMERIC" in result.skip_reason  # the property-id trap, named


# ── Shopify catalogue ──────────────────────────────────────────────


class FakeShopify:
    def __init__(self, pages: list[list[dict]]):
        self._pages = pages
        self.calls = 0

    def graphql(self, query, variables=None):
        index = self.calls
        self.calls += 1
        nodes = self._pages[index] if index < len(self._pages) else []
        return {
            "products": {
                "nodes": nodes,
                "pageInfo": {
                    "hasNextPage": index + 1 < len(self._pages),
                    "endCursor": f"cursor-{index}",
                },
            }
        }


def _product(n: int, price="0.00", tags=None):
    return {
        "id": f"gid://shopify/Product/{n}",
        "handle": f"product-{n}",
        "title": f"Product {n}",
        "productType": "SPC Vinyl Flooring",
        "vendor": "Toucan",
        "status": "ACTIVE",
        "tags": tags or [],
        "totalInventory": 5,
        "priceRangeV2": {
            "minVariantPrice": {"amount": price, "currencyCode": "CAD"},
            "maxVariantPrice": {"amount": price, "currencyCode": "CAD"},
        },
    }


def test_the_catalogue_sync_pages_through_every_product(dashboard_db):
    client = FakeShopify([[_product(1), _product(2)], [_product(3)]])
    result = sync_shopify_catalog(client=client)
    assert result.detail["pages"] == 2
    with get_session() as session:
        assert session.query(ShopifyProduct).count() == 3


def test_rerunning_the_catalogue_sync_updates_rather_than_duplicates(dashboard_db):
    sync_shopify_catalog(client=FakeShopify([[_product(1, price="0.00")]]))
    sync_shopify_catalog(client=FakeShopify([[_product(1, price="42.50")]]))
    with get_session() as session:
        rows = session.query(ShopifyProduct).all()
    assert len(rows) == 1
    assert rows[0].price_min == 42.50


def test_a_zero_price_is_recorded_as_hidden_not_missing(dashboard_db):
    """94% of this catalogue sits at 0.00 because the Orichi app hides prices.
    The zero is the fact — it's what the show-price rollout will change."""
    sync_shopify_catalog(client=FakeShopify([[_product(1), _product(2, price="30.00")]]))
    with get_session() as session:
        by_handle = {p.handle: p for p in session.query(ShopifyProduct).all()}
    assert by_handle["product-1"].has_visible_price is False
    assert by_handle["product-2"].has_visible_price is True


def test_a_product_that_vanishes_is_flagged_not_deleted(dashboard_db):
    sync_shopify_catalog(client=FakeShopify([[_product(1), _product(2)]]))
    result = sync_shopify_catalog(client=FakeShopify([[_product(1)]]))
    assert result.detail["not_seen_this_run"] == 1
    with get_session() as session:
        # Still there. A row that silently vanished couldn't distinguish
        # "delisted" from "the sync only got half the pages".
        assert session.query(ShopifyProduct).count() == 2


# ── Windsor ────────────────────────────────────────────────────────


class FakeWindsor(WindsorClient):
    def __init__(self, rows, *, enabled=True):
        self._rows = rows
        self._enabled = enabled
        self.fetches: list[tuple[date, date]] = []

    @property
    def enabled(self):
        return self._enabled

    def fetch(self, start, end):
        self.fetches.append((start, end))
        return self._rows


def _ad_row(day: date, campaign="Store Goal PMax", spend=9.69, conv=4):
    return {
        "date": day.isoformat(), "campaign": campaign,
        "campaign_type": "PERFORMANCE_MAX", "campaign_status": "ENABLED",
        "spend": spend, "clicks": 8, "impressions": 128, "conversions": conv,
    }


def test_windsor_without_a_key_is_skipped_with_the_mcp_caveat(dashboard_db):
    result = sync_ads_windsor(client=FakeWindsor([], enabled=False), today=TODAY)
    assert result.skipped is True
    assert "WINDSOR_API_KEY" in result.skip_reason
    # The trap worth naming: connecting the MCP does not configure the app.
    assert "MCP" in result.skip_reason


def test_windsor_rows_land_with_fractional_conversions(dashboard_db, monkeypatch):
    """Google attributes partial conversions and this account really does
    report 1.5. Rounding on the way in would change the numbers."""
    monkeypatch.setattr(store, "get", _stub_store({
        store.ADS_BACKFILL_DAYS: 3, store.ADS_RECENT_DAYS: 2,
    }))
    sync_ads_windsor(
        client=FakeWindsor([_ad_row(SETTLED, conv=1.5)]), today=TODAY
    )
    with get_session() as session:
        row = session.query(AdsCampaignDaily).one()
    assert row.conversions == 1.5
    assert row.source == "windsor"


def test_a_windsor_sync_leaves_direct_api_rows_alone(dashboard_db, monkeypatch):
    """Both pipes will run during the changeover to the direct Google Ads API.
    A Windsor sync must not delete rows it didn't write."""
    monkeypatch.setattr(store, "get", _stub_store({
        store.ADS_BACKFILL_DAYS: 3, store.ADS_RECENT_DAYS: 3,
    }))
    with get_session() as session:
        session.add(AdsCampaignDaily(
            date=SETTLED, campaign="Store Goal PMax", spend=1.0, source="google_ads"
        ))
    sync_ads_windsor(client=FakeWindsor([_ad_row(SETTLED)]), today=TODAY)
    with get_session() as session:
        sources = sorted(s for (s,) in session.query(AdsCampaignDaily.source).all())
    assert sources == ["google_ads", "windsor"]


def test_windsor_rerun_does_not_double_count(dashboard_db, monkeypatch):
    monkeypatch.setattr(store, "get", _stub_store({
        store.ADS_BACKFILL_DAYS: 3, store.ADS_RECENT_DAYS: 3,
    }))
    rows = [_ad_row(SETTLED)]
    sync_ads_windsor(client=FakeWindsor(rows), today=TODAY)
    sync_ads_windsor(client=FakeWindsor(rows), today=TODAY)
    with get_session() as session:
        assert session.query(AdsCampaignDaily).count() == 1


# ── The product / search-metrics join ──────────────────────────────


@pytest.mark.parametrize("stored,reported", [
    ("https://drflooring.ca/products/x", "https://drflooring.ca/products/x/"),
    ("https://drflooring.ca/products/x", "https://www.drflooring.ca/products/x"),
    ("https://drflooring.ca/products/x", "http://drflooring.ca/products/X"),
    ("https://drflooring.ca/products/x", "https://drflooring.ca/products/x?v=1"),
])
def test_products_join_search_metrics_despite_url_variation(
    dashboard_db, stored, reported
):
    """Google reports the canonical URL, which won't match ours character for
    character. A join that matches nothing is indistinguishable from a
    catalogue with no search traffic."""
    with get_session() as session:
        session.add(ShopifyProduct(
            product_gid="gid://shopify/Product/1", handle="x", title="X",
            online_url=stored,
        ))
        session.add(GscPageDaily(
            date=SETTLED, page=reported, clicks=7, impressions=100,
            ctr=0.07, position=5.0,
        ))
    data = reporting.products(window_days=7, today=TODAY)
    assert data["rows"][0]["current"].clicks == 7


def test_products_with_no_traffic_sort_last_by_position(dashboard_db):
    """Position 0 means 'never shown', not 'ranked first' — sorting it as
    rank 1 would put every untouched product at the top of the best list."""
    with get_session() as session:
        session.add(ShopifyProduct(
            product_gid="gid://1", handle="ranked", title="Ranked",
            online_url="https://drflooring.ca/products/ranked",
        ))
        session.add(ShopifyProduct(
            product_gid="gid://2", handle="unseen", title="Unseen",
            online_url="https://drflooring.ca/products/unseen",
        ))
        session.add(GscPageDaily(
            date=SETTLED, page="https://drflooring.ca/products/ranked",
            clicks=1, impressions=50, ctr=0.02, position=12.0,
        ))
    data = reporting.products(window_days=7, order="position", today=TODAY)
    assert [r["product"].handle for r in data["rows"]] == ["ranked", "unseen"]


def test_cohort_membership_filters_the_product_list(dashboard_db):
    from dashboard.models import Experiment

    with get_session() as session:
        for n in (1, 2):
            session.add(ShopifyProduct(
                product_gid=f"gid://{n}", handle=f"p{n}", title=f"P{n}",
                online_url=f"https://drflooring.ca/products/p{n}",
            ))
        experiment = Experiment(name="seo-pilot", variable="title")
        session.add(experiment)
        session.flush()
        experiment_id = experiment.id
        session.add(ExperimentProduct(
            experiment_id=experiment_id, cohort="treatment", product_gid="gid://1"
        ))
    data = reporting.products(
        window_days=7, experiment=experiment_id, cohort="treatment", today=TODAY
    )
    assert [r["product"].handle for r in data["rows"]] == ["p1"]
    assert data["rows"][0]["cohort"] == "treatment"


# ── Ads reporting ──────────────────────────────────────────────────


def test_ads_overview_puts_paid_next_to_organic_and_calls(dashboard_db):
    with get_session() as session:
        session.add(AdsCampaignDaily(
            date=SETTLED, campaign="PMax", spend=100.0, clicks=50,
            impressions=1000, conversions=5.0,
        ))
        session.add(GscPageDaily(
            date=SETTLED, page="https://drflooring.ca/", clicks=80,
            impressions=2000, ctr=0.04, position=9.0,
        ))
        session.add(Ga4EventDaily(
            date=SETTLED, event_name="call_click", event_count=3
        ))
    from dashboard.models import GscSiteDaily
    with get_session() as session:
        session.add(GscSiteDaily(
            date=SETTLED, clicks=80, impressions=2000, ctr=0.04, position=9.0
        ))

    ads = reporting.ads_overview(window_days=7, today=TODAY)
    assert ads["spend"] == 100.0
    assert ads["cpc"] == pytest.approx(2.0)
    assert ads["cost_per_conversion"] == pytest.approx(20.0)
    assert ads["organic_clicks"] == 80
    assert ads["calls"] == 3
    # The caveat has to travel with the numbers, not live in a docstring.
    assert "not a funnel" in ads["attribution_note"]


def test_spend_rising_is_not_reported_as_good(dashboard_db):
    """Spend going up is not an achievement. It's the one Ads metric where
    the pleasant-looking direction is the expensive one."""
    with get_session() as session:
        session.add(AdsCampaignDaily(
            date=SETTLED, campaign="PMax", spend=200.0, clicks=10,
        ))
        session.add(AdsCampaignDaily(
            date=SETTLED - timedelta(days=7), campaign="PMax", spend=100.0, clicks=10,
        ))
    ads = reporting.ads_overview(window_days=7, today=TODAY)
    spend = next(d for d in ads["deltas"] if d.label == "Spend")
    assert spend.direction == "up"
    assert spend.good is False


def test_a_call_baseline_that_predates_the_tracking_is_flagged(dashboard_db):
    """The GTM conversion tags went live partway through July 2026. A window
    reaching back before that produces a huge, meaningless percentage rise."""
    with get_session() as session:
        session.add(Ga4EventDaily(
            date=SETTLED, event_name="call_click", event_count=9
        ))
    ads = reporting.ads_overview(window_days=7, today=TODAY)
    assert ads["calls_baseline_incomplete"] is True
    assert ads["first_event_day"] == SETTLED


def test_a_fully_covered_baseline_is_not_flagged(dashboard_db):
    with get_session() as session:
        for n in range(0, 20):
            session.add(Ga4EventDaily(
                date=SETTLED - timedelta(days=n), event_name="call_click",
                event_count=1,
            ))
    ads = reporting.ads_overview(window_days=7, today=TODAY)
    assert ads["calls_baseline_incomplete"] is False
