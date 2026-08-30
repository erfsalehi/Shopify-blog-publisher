"""The store's naming standard, and markup that survives leaving the app.

Two of these are about a boundary. Everything `render_description` produces
is written into a Shopify product description and rendered by a theme that
has never heard of this codebase — so a class name is a wish, and layout
that matters has to travel with the content.

The naming standard is assembled rather than requested. A format a model is
asked to follow is a format that holds for most of a catalogue, and "most"
is not a standard.
"""

from __future__ import annotations

from dashboard import product_copy, store
from dashboard.product_copy import compose_title

TITLE_PARTS = {
    "brand": "EUROSTYLE",
    "collection": "Venice Grand PRO",
    "product_type": "Waterproof Luxury Vinyl Plank",
    "color": "Bassano",
}


# ── Brand + Collection + Type + Colour ─────────────────────────────


def test_the_standard_is_produced_exactly(dashboard_db):
    assert compose_title(**TITLE_PARTS) == (
        "EUROSTYLE Venice Grand PRO Waterproof Luxury Vinyl Plank - Bassano"
    )


def test_a_missing_part_leaves_no_gap_or_stray_dash(dashboard_db):
    assert compose_title(
        brand=None, collection="3D Bars", product_type="Wall Tile",
        color="Emerald Gloss",
    ) == "3D Bars Wall Tile - Emerald Gloss"


def test_a_brand_repeated_in_the_range_name_is_not_said_twice(dashboard_db):
    """Manufacturers publish a range both ways. "EUROSTYLE EUROSTYLE Venice"
    is the result of trusting the range name as given."""
    assert compose_title(
        **{**TITLE_PARTS, "collection": "EUROSTYLE Venice Grand PRO"}
    ) == "EUROSTYLE Venice Grand PRO Waterproof Luxury Vinyl Plank - Bassano"


def test_a_colour_already_in_the_range_name_is_not_repeated(dashboard_db):
    assert compose_title(
        brand="Ames", collection="Emerald Bars", product_type="Tile",
        color="Emerald Bars",
    ) == "Ames Emerald Bars Tile"


def test_without_a_colour_the_range_does_not_collapse_to_one_product(
    dashboard_db,
):
    """The hazard the fallback exists for. The colour is the only part that
    separates siblings, so composing without one gives every product in a
    range the same name — and the handle comes from the name, so the second
    onward would be skipped as already in the store. A range silently
    importing as a single product is far worse than an off-standard name.
    """
    common = {"brand": "Ames Tile", "collection": "3D Bars",
              "product_type": "Wall Tile", "color": ""}
    first = compose_title(**common, fallback="3D Bars White")
    second = compose_title(**common, fallback="3D Bars Black")
    assert first != second


def test_an_overlong_title_is_cut_rather_than_shipped(dashboard_db):
    title = compose_title(
        brand="B" * 80, collection="C" * 80, product_type="T" * 80,
        color="Bassano",
    )
    assert len(title) <= product_copy.MAX_TITLE


# ── Markup that leaves the app ─────────────────────────────────────


RELATED = [{
    "url": "https://drflooring.ca/products/deep-blue",
    "title": '4D Max | 16"x 32" Deep Blue Chevron',
    "image": "https://cdn.test/blue.jpg",
}]


def test_the_related_list_carries_its_own_layout(dashboard_db):
    """It rendered as a bulleted list of full-width images because the theme
    has no rule for `.related-grid`. Whatever the theme's CSS is, these
    rules ship with the content."""
    html = product_copy.render_related(RELATED, "4D Max")
    assert "display:grid" in html
    assert "list-style:none" in html


def test_the_thumbnails_are_constrained(dashboard_db):
    """The symptom in the screenshot: one enormous picture per row."""
    html = product_copy.render_related(RELATED, "4D Max")
    assert "width:100%" in html
    assert "object-fit:cover" in html


def test_the_layout_sets_no_colours_or_fonts(dashboard_db):
    """Layout only, so the block still inherits the storefront's typography
    instead of fighting it."""
    html = product_copy.render_related(RELATED, "4D Max")
    for property_ in ("color:", "font-family:", "background:"):
        assert property_ not in html


# ── No route off our own product page ──────────────────────────────


def test_the_manufacturers_page_is_not_linked(dashboard_db):
    """A link on our product page sending a ready-to-buy customer to the
    manufacturer is the one link a retailer should not publish."""
    copy = product_copy.ProductCopy(
        title="Oak", product_type="Flooring", summary="Oak.",
        seo_title="Oak", seo_description="Oak flooring.",
    )
    body = product_copy.render_description(
        copy, source_url="https://maker.test/products/oak"
    )
    assert "maker.test" not in body
    assert "product documentation" not in body


# ── The line that asks for the call ────────────────────────────────


def test_the_banner_opens_the_description(dashboard_db):
    """First, because it is the only line on the page asking for the
    conversion this store actually has."""
    copy = product_copy.ProductCopy(
        title="Oak", product_type="Flooring", summary="Oak flooring.",
        seo_title="Oak", seo_description="Oak flooring.",
    )
    body = product_copy.render_description(
        copy, banner=product_copy.banner_text()
    )
    assert body.index("532-2211") < body.index("Oak flooring.")


def test_the_phone_number_ships_by_default(dashboard_db):
    assert "(604) 532-2211" in product_copy.banner_text()


def test_a_pasted_entity_is_not_published_literally(dashboard_db):
    """The owner's text arrived as "&nbsp;For SPECIAL prices..." — pasted out
    of an old product page. Escaping that directly shows the customer the
    characters "&nbsp;"."""
    html = product_copy.render_banner("&nbsp;For SPECIAL prices, call NOW")
    assert "&amp;nbsp;" not in html
    assert "For SPECIAL prices" in html


def test_decoding_first_still_cannot_emit_markup(dashboard_db):
    """Unescape-then-escape looks like a no-op that could open a hole. It
    can't: the decode produces characters, and the encode puts them back."""
    html = product_copy.render_banner("&lt;script&gt;alert(1)&lt;/script&gt;")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_an_empty_banner_adds_nothing(dashboard_db):
    store.set(store.IMPORT_TOP_BANNER, "")
    copy = product_copy.ProductCopy(
        title="Oak", product_type="Flooring", summary="Oak.",
        seo_title="Oak", seo_description="Oak flooring.",
    )
    body = product_copy.render_description(
        copy, banner=product_copy.banner_text()
    )
    assert "product-banner" not in body


def test_the_number_is_editable_without_a_deploy(dashboard_db):
    store.set(store.IMPORT_TOP_BANNER, "Call (604) 000-0000")
    assert "000-0000" in product_copy.render_banner(product_copy.banner_text())


# ── Size ───────────────────────────────────────────────────────────


def test_the_size_sits_with_the_type(dashboard_db):
    assert compose_title(
        brand="Ames Tile", collection="3D Bars",
        product_type="Porcelain Wall Tile", size='5"x10"',
        color="Emerald Bevel Gloss",
    ) == 'Ames Tile 3D Bars Porcelain Wall Tile 5"x10" - Emerald Bevel Gloss'


def test_a_range_sold_in_one_size_does_not_grow_one(dashboard_db):
    """Omitted when the source doesn't state it, rather than guessed at."""
    assert compose_title(**TITLE_PARTS, size="") == (
        "EUROSTYLE Venice Grand PRO Waterproof Luxury Vinyl Plank - Bassano"
    )


def test_a_size_already_in_the_type_is_not_said_twice(dashboard_db):
    assert compose_title(
        brand="Ames", collection="3D Bars", product_type='Wall Tile 5"x10"',
        size='5"x10"', color="Jade",
    ) == 'Ames 3D Bars Wall Tile 5"x10" - Jade'


# ── What a range is called on the shelf ────────────────────────────


def test_the_collection_standard_is_produced_exactly(dashboard_db):
    """The store's existing collections read this way — "Cyrus Luxury Vinyl
    Collection", "Simba Engineered Flooring Collection"."""
    assert product_copy.compose_collection_title(
        brand="Cyrus", collection="Luxury Vinyl"
    ) == "Cyrus Luxury Vinyl Collection"


def test_the_handle_matches_the_stores_existing_ones(dashboard_db):
    """`cyrus-craftsman-collection` is a handle that already exists. A new
    import of that range must land on it rather than beside it."""
    title = product_copy.compose_collection_title(
        brand="Cyrus", collection="Craftsman"
    )
    assert product_copy.slugify(title) == "cyrus-craftsman-collection"


def test_a_brand_repeated_in_the_range_name_is_not_said_twice(dashboard_db):
    assert product_copy.compose_collection_title(
        brand="Cyrus", collection="Cyrus Craftsman"
    ) == "Cyrus Craftsman Collection"


def test_the_word_collection_is_not_added_twice(dashboard_db):
    """A manufacturer publishing a range as "Craftsman Collection" should not
    become "Craftsman Collection Collection"."""
    assert product_copy.compose_collection_title(
        brand="Simba", collection="Engineered Flooring Collection"
    ) == "Simba Engineered Flooring Collection"


def test_without_a_brand_the_range_still_gets_a_name(dashboard_db):
    assert product_copy.compose_collection_title(
        brand=None, collection="3D Bars"
    ) == "3D Bars Collection"


def test_the_shelf_name_is_not_the_tag(dashboard_db):
    """The tag is what a smart collection is defined on and what the "more
    from this range" heading reads. "Ames Tile & Stone 3D Bars Collection" is
    a mouthful in a heading and a filter, so the two are composed apart."""
    shelf = product_copy.compose_collection_title(
        brand="Ames Tile & Stone", collection="3D Bars"
    )
    heading = product_copy.render_related(RELATED, "3D Bars")
    assert shelf == "Ames Tile & Stone 3D Bars Collection"
    assert "from 3D Bars," in heading
    assert "Collection," not in heading
