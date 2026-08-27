"""Importing a range the store already carries.

The half of an import that isn't the first import. A supplier's collection
is re-read months later: some of it is already in the store, some is new,
and some of what's in the store is no longer in their catalogue. Each of
those needs a different answer, and the destructive reading of any of them
is the one that loses work.

The rules this pins down:

  * A product already in the store is left exactly as it is, and is still
    put in the collection. It may exist without ever having been in one.
  * A product the supplier has dropped stays. Their catalogue is a record of
    what they sell, not of what we do.
  * An existing collection page is added to, never rewritten — it may have
    been edited by hand since it was made.
  * Whether a page gets built at all is the owner's call, because a range
    can be maintained as a manual collection or as a smart one built on the
    brand and collection tags.
"""

from __future__ import annotations

import json

import pytest

from dashboard import product_import
from dashboard.db import get_session
from dashboard.models import (
    ImportProduct,
    ImportProductStatus,
    ImportRun,
    ImportStage,
)


class FakeShopify:
    """Just enough Shopify to watch what the collection stage decides."""

    def __init__(self, collections=None):
        self.collections = list(collections or [])
        self.created: list[dict] = []
        self.added: list[tuple[str, list[str]]] = []

    def find_collection(self, handle):
        return next((c for c in self.collections if c["handle"] == handle), None)

    def create_collection(self, **kwargs):
        self.created.append(kwargs)
        row = {"id": "gid://shopify/Collection/1", **kwargs}
        self.collections.append(row)
        return row

    def add_products_to_collection(self, gid, product_gids):
        self.added.append((gid, list(product_gids)))

    def close(self):
        pass


@pytest.fixture
def run_factory(dashboard_db, monkeypatch):
    """Build a run sitting at the collection stage with a known product mix."""
    def build(*, mode="new", build_page=True, created=2, skipped=0, shopify=None):
        fake = shopify or FakeShopify()
        monkeypatch.setattr(product_import, "_shopify_client", lambda: fake)
        with get_session() as session:
            run = ImportRun(
                source_url="https://maker.test/collections/berlin",
                source_base="https://maker.test",
                collection_title="Berlin Series",
                collection_handle="berlin-series",
                stage=ImportStage.collection.value,
                options_json=json.dumps({
                    "collection_mode": mode,
                    "build_page": build_page,
                    "link_products": False,
                }),
            )
            session.add(run)
            session.flush()
            n = 0
            for status, count in (
                (ImportProductStatus.created.value, created),
                (ImportProductStatus.skipped.value, skipped),
            ):
                for _ in range(count):
                    n += 1
                    session.add(ImportProduct(
                        run_id=run.id, position=n,
                        source_url=f"https://maker.test/p{n}",
                        title=f"Berlin {n}", status=status,
                        product_gid=f"gid://shopify/Product/{n}",
                    ))
            session.flush()
            return run.id, fake
    return build


def _note(run_id: int) -> str:
    with get_session() as session:
        return " ".join(session.get(ImportRun, run_id).log)


# ── A range we already carry ───────────────────────────────────────


def test_products_already_in_the_store_still_join_the_collection(run_factory):
    """The case that makes 'update' worth a mode of its own. A product can
    exist without ever having been in a collection — skipping it here would
    leave it out of its own range permanently."""
    run_id, fake = run_factory(mode="update", created=1, skipped=3)
    product_import.advance(run_id)

    assert len(fake.created) == 1
    assert len(fake.created[0]["product_gids"]) == 4


def test_the_note_says_how_many_were_already_ours(run_factory):
    """"12 products" reads the same whether all twelve are new or none are."""
    run_id, _ = run_factory(mode="update", created=2, skipped=5)
    product_import.advance(run_id)
    assert "2 new, 5 already in the store" in _note(run_id)


def test_an_existing_page_is_added_to_not_rewritten(run_factory):
    """It may have been edited by hand since it was made. A re-import that
    restored a generated description would throw that away silently."""
    fake = FakeShopify([
        {"id": "gid://shopify/Collection/9", "handle": "berlin-series",
         "title": "Berlin Series (edited by hand)"},
    ])
    run_id, fake = run_factory(mode="update", shopify=fake)
    product_import.advance(run_id)

    assert fake.created == []
    assert fake.added and fake.added[0][0] == "gid://shopify/Collection/9"


def test_a_supplier_dropping_a_product_does_not_remove_ours(run_factory):
    """Nothing in this stage deletes. Their catalogue is a record of what
    they sell, not of what we do."""
    fake = FakeShopify([
        {"id": "gid://shopify/Collection/9", "handle": "berlin-series",
         "title": "Berlin Series"},
    ])
    run_id, fake = run_factory(mode="update", shopify=fake)
    product_import.advance(run_id)

    assert not hasattr(fake, "removed")
    assert fake.added[0][1]  # only ever adds


# ── Whether a page gets built ──────────────────────────────────────


def test_declining_the_page_creates_no_collection(run_factory):
    run_id, fake = run_factory(mode="update", build_page=False)
    product_import.advance(run_id)
    assert fake.created == []


def test_declining_the_page_still_says_the_tags_will_carry_it(run_factory):
    """Not a dead end: brand and collection tags are guaranteed on every
    product, so a smart collection built on those picks them up."""
    run_id, _ = run_factory(mode="update", build_page=False)
    product_import.advance(run_id)
    assert "tags" in _note(run_id)


def test_declining_the_page_does_not_stall_the_run(run_factory):
    run_id, _ = run_factory(mode="update", build_page=False)
    product_import.advance(run_id)
    with get_session() as session:
        assert session.get(ImportRun, run_id).stage != ImportStage.collection.value


# ── A range we don't ───────────────────────────────────────────────


def test_a_new_range_gets_its_page(run_factory):
    run_id, fake = run_factory(mode="new", created=3)
    product_import.advance(run_id)
    assert len(fake.created) == 1
    assert fake.created[0]["handle"] == "berlin-series"


def test_calling_it_new_when_it_is_not_says_so_rather_than_duplicating(
    run_factory,
):
    """The owner picked wrong. Making a second page for a range that already
    has one is the expensive way to be right about the label."""
    fake = FakeShopify([
        {"id": "gid://shopify/Collection/9", "handle": "berlin-series",
         "title": "Berlin Series"},
    ])
    run_id, fake = run_factory(mode="new", shopify=fake)
    product_import.advance(run_id)

    assert fake.created == []
    assert "already existed" in _note(run_id)
