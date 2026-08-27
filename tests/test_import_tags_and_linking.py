"""Tags the storefront is built on, and linking a whole range together.

The store builds collection pages out of tags: a page is defined as "brand X
and collection Y", and anything carrying both appears on it. That only works
if both tags are on every product in the range every time — a product the
model happened to tag differently is a product missing from its own
collection page, and nothing about the page says so.

Which makes the ordering in `_required_tags` the whole test. `clean_tags`
caps a product at `MAX_TAGS` and the model routinely proposes fifteen, so
tags appended after the model's would be the first thing dropped.
"""

from __future__ import annotations

from dashboard import product_copy, store
from dashboard.product_copy import MAX_TAGS, clean_tags
from dashboard.product_import import _related_limit, _required_tags


def _tags(**kwargs):
    base = {
        "product_type": "Wall Tile",
        "vendor": "Ames Tile",
        "collection_title": "3D Bars",
        "source_tag": "imported",
    }
    base.update(kwargs)
    return _required_tags(**base)


# ── The tags the store depends on ──────────────────────────────────


def test_brand_type_and_collection_are_all_present(dashboard_db):
    assert _tags() == ["3D Bars", "Ames Tile", "Wall Tile", "imported"]


def test_a_talkative_model_cannot_push_the_brand_off_the_list(dashboard_db):
    """The failure this ordering exists to prevent. Twenty invented tags plus
    four required ones is over the cap, and whichever end is truncated
    decides whether the product appears on its own collection page."""
    invented = [f"tag-{n}" for n in range(MAX_TAGS + 5)]
    final = clean_tags(_tags() + invented)

    assert len(final) == MAX_TAGS
    for required in ("3D Bars", "Ames Tile", "Wall Tile", "imported"):
        assert required in final


def test_a_missing_vendor_does_not_leave_a_blank_tag(dashboard_db):
    """An empty tag is a filter nobody can click and a row in the admin that
    looks like corruption."""
    assert _tags(vendor=None, source_tag="") == ["3D Bars", "Wall Tile"]


def test_the_collection_tag_survives_a_model_that_already_guessed_it(
    dashboard_db,
):
    """`clean_tags` dedupes case-insensitively, so the required one must not
    become a second, differently-cased entry."""
    final = clean_tags(_tags() + ["3d bars", "ames tile"])
    assert final.count("3D Bars") == 1
    assert "3d bars" not in final


# ── Linking the range ──────────────────────────────────────────────


def test_every_product_in_a_normal_range_is_linked(dashboard_db):
    """It used to be eight. A flooring series runs to a few dozen, and a
    range that only half links itself is half a range to a crawler."""
    assert _related_limit() >= 40


def test_the_limit_is_a_setting(dashboard_db):
    store.set(store.IMPORT_RELATED_LIMIT, 12)
    assert _related_limit() == 12


def test_the_heading_names_the_range(dashboard_db):
    """"More from this collection" tells a reader nothing they could search
    for; the range's own name is the phrase they would type."""
    html = product_copy.render_related(
        [{"url": "https://x.test/p2", "title": "Berlin Oak", "image": None}],
        "100% Waterproof SPC Luxury Vinyl Plank Berlin Series",
    )
    assert "Berlin Series" in html
    assert "you can find them in the following list" in html


def test_a_range_with_no_name_still_gets_a_heading(dashboard_db):
    html = product_copy.render_related(
        [{"url": "https://x.test/p2", "title": "Oak", "image": None}], ""
    )
    assert "More from this collection" in html


def test_a_collection_name_cannot_inject_markup(dashboard_db):
    """The title is editable on the import form, and it lands in a heading on
    every product page in the range."""
    html = product_copy.render_related(
        [{"url": "https://x.test/p2", "title": "Oak", "image": None}],
        "<script>alert(1)</script>",
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_each_linked_product_carries_its_picture_and_title(dashboard_db):
    html = product_copy.render_related(
        [{"url": "https://x.test/p2", "title": "Berlin Oak",
          "image": "https://x.test/oak.jpg"}],
        "Berlin Series",
    )
    assert 'href="https://x.test/p2"' in html
    assert 'src="https://x.test/oak.jpg"' in html
    assert "Berlin Oak" in html
