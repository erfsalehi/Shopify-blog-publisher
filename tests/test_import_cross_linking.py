"""Every product in a range links to the whole range.

The grid is the range's internal linking — how a collection reads as a range
rather than as unrelated pages, to a shopper and to a crawler. So the
property worth pinning is not "there are some links" but "the links are the
whole range", and it has to hold on a re-import, which is when most of the
range already exists.

It did not. `_linking` collected only products with status `created`, so what
each product linked to was not the range but whichever part of the range that
particular run happened to create. Re-importing thirteen products of which
eleven already existed left every product linking to exactly one other, and
the log reported "Cross-linked 2 products" as though that were the whole job.
`_collection` had always used both statuses; the same question, answered two
ways in one file.
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


class LinkingShopify:
    """Records the description written to each product."""

    def __init__(self):
        self.bodies: dict[str, str] = {}
        self.metafields: list[dict] = []
        self.definitions: dict[str, str] = {}

    def update_product(self, gid, **kwargs):
        if "description_html" in kwargs:
            self.bodies[gid] = kwargs["description_html"]
        return {"id": gid}

    #: Definitions the store has, keyed by metafield key. The importer asks
    #: for these before it writes: a value written under a key the store has
    #: not defined is stored by Shopify and shown by nothing, which is what
    #: an empty Metafields panel on a fully imported product means.
    def all_metafield_definitions(self, owner_type="PRODUCT"):
        """Every definition in every namespace — what answers "what does my
        store call these", which a namespace guess cannot."""
        return [
            {"namespace": "custom", "key": key, "name": key, "type": type_}
            for key, type_ in self.definitions.items()
        ]

    def metafield_definitions(self, namespace, owner_type="PRODUCT"):
        """{key: type} the store has defined. A key absent from here is a
        key the importer will not write into: a filter definition is the
        merchant's, and filling a lookalike beside it leaves the real filter
        empty."""
        return dict(self.definitions)

    def ensure_metafield_definitions(self, definitions, *, namespace,
                                     owner_type="PRODUCT"):
        created = []
        conflicting = []
        for definition in definitions:
            key, wanted = definition["key"], definition["type"]
            existing = self.definitions.get(key)
            if existing is None:
                self.definitions[key] = wanted
                created.append(key)
            elif existing != wanted:
                conflicting.append(f"{key} (defined as {existing})")
        return created, conflicting

    def set_metafields(self, metafields):
        self.metafields.extend(metafields)
        return metafields

    def close(self):
        pass


COPY = {
    "title": "A tile", "product_type": "Wall Tile", "summary": "A tile.",
    "paragraphs": [], "features": [], "specs": [], "applications": [],
    "faqs": [], "tags": [], "seo_title": "A tile",
    "seo_description": "A tile from the range.", "image_alts": [],
}


@pytest.fixture
def range_run(dashboard_db, monkeypatch):
    """A run whose products are part created, part already in the store."""
    def build(*, created: int, skipped: int):
        fake = LinkingShopify()
        monkeypatch.setattr(product_import, "_shopify_client", lambda: fake)
        with get_session() as session:
            run = ImportRun(
                source_url="https://maker.test/collections/bars",
                source_base="https://maker.test",
                vendor="Ames Tile",
                collection_title="3D Bars",
                collection_handle="ames-tile-3d-bars-collection",
                stage=ImportStage.linking.value,
                options_json=json.dumps({"link_products": True}),
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
                        run_id=run.id, position=n, status=status,
                        source_url=f"https://maker.test/p{n}",
                        title=f"3D Bars Shade {n}",
                        handle=f"3d-bars-shade-{n}",
                        product_gid=f"gid://shopify/Product/{n}",
                        generated_json=json.dumps({**COPY, "title": f"Shade {n}"}),
                        extracted_json=json.dumps({
                            "images": [{"url": f"https://cdn.test/{n}.jpg"}]
                        }),
                    ))
            session.flush()
            return run.id, fake
    return build


def _finish(run_id: int) -> None:
    """Advance until the run is done. One pass is bounded by wall clock, so a
    thirteen-product range does not finish in a single call — and the closing
    note is only written on the pass that finishes."""
    for _ in range(30):
        result = product_import.advance(run_id)
        if result.done:
            return
    raise AssertionError("run did not finish")


def _note(run_id: int) -> str:
    with get_session() as session:
        return " ".join(session.get(ImportRun, run_id).log)


def test_a_product_links_to_every_other_in_the_range(range_run):
    run_id, fake = range_run(created=6, skipped=0)
    _finish(run_id)

    for gid, body in fake.bodies.items():
        linked = [n for n in range(1, 7) if f'href="/products/3d-bars-shade-{n}"' in body]
        assert len(linked) == 5, f"{gid} linked {len(linked)} of its 5 siblings"


def test_products_already_in_the_store_are_in_the_grid(range_run):
    """The reported symptom. Eleven of thirteen already existed, and every
    product came out linking to exactly one other."""
    run_id, fake = range_run(created=2, skipped=11)
    _finish(run_id)

    body = next(iter(fake.bodies.values()))
    linked = [n for n in range(1, 14) if f'href="/products/3d-bars-shade-{n}"' in body]
    assert len(linked) == 12


def test_products_already_in_the_store_get_a_grid_of_their_own(range_run):
    """Not only listed in other products' grids — they need one themselves,
    or eleven of thirteen pages are still dead ends."""
    run_id, fake = range_run(created=2, skipped=11)
    _finish(run_id)
    assert len(fake.bodies) == 13


def test_rewriting_an_existing_products_description_is_reported(range_run):
    """It was reported "left untouched" a minute earlier, and this does touch
    it. A log that says otherwise is worse than the rewrite."""
    run_id, _ = range_run(created=2, skipped=11)
    _finish(run_id)
    note = _note(run_id)
    assert "Cross-linked 13 products" in note
    assert "11 of them were already in the store" in note


def test_a_range_of_one_is_not_cross_linked(range_run):
    run_id, fake = range_run(created=1, skipped=0)
    _finish(run_id)
    assert fake.bodies == {}
    assert "nothing to cross-link" in _note(run_id)


def test_the_grid_carries_a_picture_for_each_sibling(range_run):
    run_id, fake = range_run(created=4, skipped=0)
    _finish(run_id)
    body = next(iter(fake.bodies.values()))
    assert body.count("https://cdn.test/") == 3


def test_no_product_links_to_itself(range_run):
    run_id, fake = range_run(created=5, skipped=0)
    _finish(run_id)
    with get_session() as session:
        rows = {
            r.product_gid: r.handle
            for r in session.query(ImportProduct).filter(
                ImportProduct.run_id == run_id
            )
        }
    for gid, body in fake.bodies.items():
        assert f'href="/products/{rows[gid]}"' not in body
