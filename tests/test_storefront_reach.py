"""Whether what the importer makes can actually be reached.

September 2026: products imported from the 7th onward were almost never
indexed. Every product page was live. What wasn't: the range collections,
which the importer created on no sales channel (so they 404'd), and the
brand page, which filtered on a vendor spelled differently from the one the
imports typed and listed its ranges in hand-written theme HTML. Each of these
is a way for a page to exist with nothing Google can reach linking to it.
"""

from __future__ import annotations

from datetime import date

import pytest

from blog_pipeline.tools.shopify import ShopifyClient, _menu_item_input
from dashboard import alerts, product_copy, product_import
from dashboard.db import get_session
from dashboard.models import AlertRule, AppSetting, ImportRun


# ── The store's own spelling of a brand ────────────────────────────


STORE = ["Ames Tile & Stone", "D & R Flooring"]


@pytest.mark.parametrize("typed", [
    "Ames Tile",                # the one that cost 120 products their brand page
    "ames tile & stone",
    "Ames Tile and Stone",
    "Ames Tile & Stone Ltd",
])
def test_a_brand_the_store_already_has_is_written_its_way(typed):
    assert product_copy.store_vendor(typed, STORE) == "Ames Tile & Stone"


def test_a_new_brand_is_left_as_typed():
    assert product_copy.store_vendor("Mohawk", STORE) == "Mohawk"


def test_two_candidates_is_a_guess_not_a_match():
    known = ["Ames Tile & Stone", "Ames Tile Outlet"]
    assert product_copy.store_vendor("Ames Tile", known) == "Ames Tile"


def test_an_exact_match_wins_over_a_longer_one():
    known = ["Toucan", "Toucan Floors"]
    assert product_copy.store_vendor("toucan", known) == "Toucan"


def test_no_vendor_stays_no_vendor():
    assert product_copy.store_vendor(None, STORE) is None
    assert product_copy.store_vendor("", STORE) == ""


# ── The Shopify client ─────────────────────────────────────────────


class Scripted(ShopifyClient):
    """A real client whose GraphQL answers come from a list, in order."""

    def __init__(self, *answers):
        super().__init__(domain="test.myshopify.com", token="x")
        self.answers = list(answers)
        self.sent: list[tuple[str, dict]] = []
        self._publications = [
            {"id": "gid://shopify/Publication/1", "name": "Online Store"},
            {"id": "gid://shopify/Publication/2", "name": "Shop"},
        ]

    def graphql(self, query, variables=None):
        self.sent.append((query, variables or {}))
        return self.answers.pop(0)


def test_only_what_the_online_store_lacks_is_reported():
    client = Scripted({"nodes": [
        {"id": "gid://shopify/Collection/1", "publishedOnPublication": False},
        {"id": "gid://shopify/Product/2", "publishedOnPublication": True},
        None,  # a deleted id comes back null, and is not "unpublished"
    ]})
    missing = client.not_on_online_store(
        ["gid://shopify/Collection/1", "gid://shopify/Product/2", "gid://shopify/Product/3"]
    )
    assert [m["id"] for m in missing] == ["gid://shopify/Collection/1"]
    assert client.sent[0][1]["pub"] == "gid://shopify/Publication/1"


def test_an_empty_collection_is_not_a_gap():
    """Publishing it would put a page with nothing on it live."""
    client = Scripted({
        "products": {"nodes": []},
        "collections": {"nodes": [
            {"id": "a", "title": "Bliss Brick", "productsCount": {"count": 19},
             "publishedOnPublication": False},
            {"id": "b", "title": "Ceramin Gallery", "productsCount": {"count": 0},
             "publishedOnPublication": False},
        ]},
    })
    gaps = client.storefront_gaps()
    assert [c["title"] for c in gaps["collections"]] == ["Bliss Brick"]


MENU = {"menus": {"nodes": [{
    "id": "gid://shopify/Menu/9", "handle": "ames-tile-stone-collections",
    "title": "Ames Tile & Stone collections",
    "items": [
        {"id": "gid://shopify/MenuItem/1", "title": "3D Bars Collection",
         "type": "COLLECTION", "resourceId": "gid://shopify/Collection/1",
         "url": "/collections/ames-tile-stone-3d-bars-collection", "tags": [],
         "items": []},
        {"id": "gid://shopify/MenuItem/2", "title": "Calema Collection",
         "type": "COLLECTION", "resourceId": "gid://shopify/Collection/2",
         "url": "/collections/ames-tile-calema-collection", "tags": [],
         "items": []},
    ],
}]}}


def test_a_range_is_slotted_into_its_brand_menu_keeping_the_rest():
    """`menuUpdate` replaces the whole list: an item not sent back is gone."""
    client = Scripted(MENU, {"menuUpdate": {"menu": {"id": "x"}, "userErrors": []}})
    outcome = client.add_collection_to_menu(
        "ames-tile-stone-collections", "gid://shopify/Collection/3", "Bliss Collection",
    )
    assert outcome == "added"
    items = client.sent[1][1]["items"]
    assert [i["title"] for i in items] == [
        "3D Bars Collection", "Bliss Collection", "Calema Collection",
    ]
    kept = items[0]
    assert kept["id"] == "gid://shopify/MenuItem/1"
    assert "url" not in kept  # Shopify derives it from the resource


def test_a_range_already_on_the_menu_is_left_alone():
    client = Scripted(MENU)
    assert client.add_collection_to_menu(
        "ames-tile-stone-collections", "gid://shopify/Collection/2", "Calema",
    ) == "present"
    assert len(client.sent) == 1


def test_no_menu_means_no_write():
    client = Scripted({"menus": {"nodes": []}})
    assert client.add_collection_to_menu(
        "toucan-collections", "gid://shopify/Collection/2", "Berlin",
    ) == "no-menu"
    assert len(client.sent) == 1


def test_a_submenu_survives_being_sent_back():
    item = {"id": "1", "title": "Tiles", "type": "HTTP", "url": "/tiles",
            "items": [{"id": "2", "title": "Wall", "type": "COLLECTION",
                       "resourceId": "c", "url": "/collections/wall"}]}
    assert _menu_item_input(item) == {
        "id": "1", "title": "Tiles", "type": "HTTP", "url": "/tiles",
        "items": [{"id": "2", "title": "Wall", "type": "COLLECTION", "resourceId": "c"}],
    }


def test_the_brand_menu_handle_follows_the_brand():
    assert product_import.brand_menu_handle("Ames Tile & Stone") == "ames-tile-stone-collections"


# ── The alert ──────────────────────────────────────────────────────


def test_a_kind_added_later_reaches_an_existing_database_once(dashboard_db):
    """A database seeded before the storefront rule existed gets it — and a
    rule the owner then deletes stays deleted."""
    with get_session() as session:
        for kind in sorted(alerts._ORIGINAL_KINDS):
            session.add(AlertRule(name=kind, kind=kind, threshold=0, enabled=True))
    assert alerts.ensure_default_rules() == 1
    with get_session() as session:
        assert session.query(AlertRule).filter(
            AlertRule.kind == "storefront_gaps").count() == 1
        session.query(AlertRule).filter(AlertRule.kind == "storefront_gaps").delete()
    assert alerts.ensure_default_rules() == 0
    with get_session() as session:
        assert not session.query(AlertRule).filter(
            AlertRule.kind == "storefront_gaps").count()
        assert session.get(AppSetting, alerts._SEEDED_KEY) is not None


def test_the_storefront_rule_is_silent_without_shopify(dashboard_db):
    assert alerts._storefront_gaps(0, date(2026, 9, 25)) == []


def test_a_collection_off_the_storefront_is_a_high_alert(dashboard_db, monkeypatch):
    import blog_pipeline.config as config
    import blog_pipeline.tools.shopify as shopify

    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "test.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "x")
    config.get_settings.cache_clear()

    class Gappy:
        def storefront_gaps(self):
            return {
                "collections": [{"title": "Ames Tile Calema Collection"}],
                "products": [],
            }

        def close(self):
            pass

    monkeypatch.setattr(shopify, "ShopifyClient", Gappy)
    findings = alerts._storefront_gaps(0, date(2026, 9, 25))
    assert len(findings) == 1
    assert findings[0].severity == "high"
    assert "Calema" in findings[0].body
    assert findings[0].title == "1 live collection not on the Online Store"


# ── The importer ───────────────────────────────────────────────────


def _note(run_id: int) -> str:
    with get_session() as session:
        return " ".join(session.get(ImportRun, run_id).log)


def test_settling_the_vendor_is_said_and_sticks(dashboard_db):
    class Vendors:
        def product_vendors(self):
            return STORE

    with get_session() as session:
        run = ImportRun(source_url="https://maker.test/c", vendor="Ames Tile",
                        options_json="{}")
        session.add(run)
        session.flush()
        run_id = run.id
    assert product_import._settle_vendor(run_id, Vendors(), "Ames Tile") == "Ames Tile & Stone"
    with get_session() as session:
        run = session.get(ImportRun, run_id)
        assert run.vendor == "Ames Tile & Stone"
        assert run.options["vendor_settled"] is True
    assert "brand page" in _note(run_id)
