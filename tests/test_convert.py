import re

import pytest

from blog_pipeline.agents.convert import (
    apply_conversion_blocks,
    pick_best,
    place_blocks,
    render_call_banner,
    render_collection_row,
    render_product_row,
    tag_url,
)
from blog_pipeline.config import get_settings

# Four sections, so there are three usable breaks (the first <h2> is reserved).
BODY = (
    "<p>Intro paragraph about floors.</p>"
    "<h2>Choosing a floor</h2><p>" + "word " * 120 + "</p>"
    "<h2>Installing it</h2><p>" + "word " * 120 + "</p>"
    "<h2>Caring for it</h2><p>" + "word " * 120 + "</p>"
    "<h2>Costs</h2><p>" + "word " * 120 + "</p>"
)

PRODUCTS = [
    {
        "title": "EUROSTYLE Venice Waterproof Luxury Vinyl Plank",
        "url": "https://drflooring.ca/products/venice-lvp",
        "image": "https://cdn.example/venice.jpg",
        "price": "$3.49",
    },
    {
        "title": "Oak Hardwood Nosing",
        "url": "https://drflooring.ca/products/oak-nosing",
        "image": "https://cdn.example/oak.jpg",
        "price": "$29.00",
    },
]

COLLECTIONS = [
    {
        "title": "Vinyl Plank Flooring",
        "url": "https://drflooring.ca/collections/vinyl",
        "image": "https://cdn.example/vinyl.jpg",
    },
    {
        "title": "Laminate Flooring",
        "url": "https://drflooring.ca/collections/laminate",
        "image": "https://cdn.example/laminate.jpg",
    },
    {
        "title": "Underlay",
        "url": "https://drflooring.ca/collections/underlay",
        "image": "https://cdn.example/underlay.jpg",
    },
]


@pytest.fixture
def phone(monkeypatch):
    monkeypatch.setenv("BUSINESS_PHONE", "(604) 555-0134")
    monkeypatch.setenv("BUSINESS_NAME", "D&R Flooring")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ── individual blocks ────────────────────────────────────────────
def test_call_banner_renders_a_dialable_tel_link(phone):
    out = render_call_banner()
    assert 'href="tel:6045550134"' in out
    assert "Call (604) 555-0134" in out
    # the business name is escaped, so the ampersand can't break the markup
    assert "D&amp;R Flooring" in out


def test_call_banner_keeps_an_international_prefix(monkeypatch):
    monkeypatch.setenv("BUSINESS_PHONE", "+1 604 555 0134")
    get_settings.cache_clear()
    try:
        assert 'href="tel:+16045550134"' in render_call_banner()
    finally:
        get_settings.cache_clear()


def test_call_banner_is_skipped_without_a_number():
    assert render_call_banner() == ""


def test_call_banner_is_skipped_when_the_number_is_too_short(monkeypatch):
    monkeypatch.setenv("BUSINESS_PHONE", "555")
    get_settings.cache_clear()
    try:
        assert render_call_banner() == ""
    finally:
        get_settings.cache_clear()


def test_product_card_carries_image_price_and_utm():
    out = render_product_row([PRODUCTS[0]], "Best Vinyl Plank 2026")
    assert "utm_medium=product-card" in out
    assert "utm_campaign=best-vinyl-plank-2026" in out
    assert "$3.49" in out
    assert 'loading="lazy"' in out


def test_product_card_needs_a_picture():
    assert render_product_row([{"title": "T", "url": "u"}], "c") == ""


def test_the_product_row_shows_several_options():
    """One product is a suggestion; three are a choice — the point of the row."""
    out = render_product_row(PRODUCTS, "Guide")
    assert out.count("<a href=") == len(PRODUCTS)
    assert 'class="product-cards"' in out


def test_collection_row_renders_each_card():
    out = render_collection_row(COLLECTIONS, "Vinyl Guide")
    assert out.count("<a href=") == 3
    assert "utm_medium=collection-card" in out


def test_collection_row_drops_entries_without_an_image():
    out = render_collection_row(
        [{"title": "No Picture", "url": "https://x/c/n"}], "Guide"
    )
    assert out == ""


def test_tag_url_appends_to_an_existing_query():
    assert "?ref=1&utm_source=blog" in tag_url("https://x/p?ref=1", "product-card", "T")
    assert "?utm_source=blog" in tag_url("https://x/p", "product-card", "T")


# ── placement ────────────────────────────────────────────────────
def test_blocks_land_on_section_breaks_never_mid_paragraph():
    out = place_blocks(BODY, [(0.3, "<div>A</div>"), (0.78, "<div>B</div>")])
    for marker in ("<div>A</div>", "<div>B</div>"):
        assert re.search(re.escape(marker) + r"<h2", out), f"{marker} not at a break"


def test_the_first_heading_is_left_alone_for_the_intro():
    out = place_blocks(BODY, [(0.0, "<div>A</div>")])
    # even aiming at the very top, the block lands after the opening section
    assert out.index("Choosing a floor") < out.index("<div>A</div>")


def test_blocks_never_share_a_break():
    body = "<h2>One</h2><p>x</p><h2>Two</h2><p>y</p><h2>Three</h2><p>z</p>"
    out = place_blocks(body, [(0.5, "<div>A</div>"), (0.5, "<div>B</div>")])
    assert "<div>A</div><div>B</div>" not in out
    assert "<div>A</div>" in out and "<div>B</div>" in out


def test_extra_blocks_are_dropped_when_breaks_run_out():
    body = "<h2>One</h2><p>x</p><h2>Two</h2><p>y</p>"  # exactly one usable break
    out = place_blocks(body, [(0.3, "<div>A</div>"), (0.8, "<div>B</div>")])
    assert "<div>A</div>" in out
    assert "<div>B</div>" not in out


def test_body_without_headings_is_returned_untouched():
    body = "<p>Just a paragraph.</p>"
    assert place_blocks(body, [(0.5, "<div>A</div>")]) == body


# ── matching ─────────────────────────────────────────────────────
def test_pick_best_prefers_the_title_matching_the_keywords():
    best = pick_best(COLLECTIONS, ["vinyl plank flooring"], count=1)
    assert best[0]["title"] == "Vinyl Plank Flooring"


def test_pick_best_falls_back_to_catalogue_order_without_keywords():
    assert pick_best(COLLECTIONS, [], count=2) == COLLECTIONS[:2]


def test_pick_best_handles_an_empty_catalogue():
    assert pick_best([], ["vinyl"], count=3) == []


# ── the whole pass ───────────────────────────────────────────────
def test_apply_adds_all_three_blocks_in_reading_order(phone):
    out = apply_conversion_blocks(
        body_html=BODY,
        campaign="Vinyl Plank Guide",
        keywords=["vinyl plank flooring"],
        products=PRODUCTS,
        collections=COLLECTIONS,
    )
    assert out.index("call-banner") < out.index("product-cards") < out.index(
        "collection-cards"
    )
    # the product picked is the one matching the article, not just the first
    assert "venice-lvp" in out


def test_apply_is_a_no_op_when_the_switch_is_off(monkeypatch):
    monkeypatch.setenv("ENABLE_CONVERSION_BLOCKS", "false")
    get_settings.cache_clear()
    try:
        out = apply_conversion_blocks(
            body_html=BODY, campaign="T", products=PRODUCTS, collections=COLLECTIONS
        )
        assert out == BODY
    finally:
        get_settings.cache_clear()


def test_apply_still_places_cards_without_a_phone_number():
    """The call banner is the only block that needs a setting; missing it must
    not cost the cards their slots."""
    out = apply_conversion_blocks(
        body_html=BODY,
        campaign="Guide",
        keywords=["vinyl"],
        products=PRODUCTS,
        collections=COLLECTIONS,
    )
    assert "call-banner" not in out
    assert "product-cards" in out and "collection-cards" in out


def test_apply_with_an_empty_catalogue_changes_nothing():
    out = apply_conversion_blocks(body_html=BODY, campaign="Guide")
    assert out == BODY


def test_a_top_up_block_does_not_stack_against_an_existing_one():
    """A block already sitting before a heading occupies that break, so a
    later run puts its block at a different one rather than beside it."""
    existing = '<div class="product-cards">existing</div>'
    body = BODY.replace("<h2>Installing it</h2>", existing + "<h2>Installing it</h2>", 1)
    out = place_blocks(body, [(0.55, "<div>NEW</div>")])
    assert "<div>NEW</div>" in out
    assert existing + "<div>NEW</div>" not in out
    assert "<div>NEW</div>" + existing not in out


def test_skip_leaves_out_the_named_blocks(monkeypatch):
    monkeypatch.setenv("BUSINESS_PHONE", "(604) 555-0134")
    get_settings.cache_clear()
    try:
        out = apply_conversion_blocks(
            body_html=BODY, campaign="Guide", keywords=["vinyl"],
            products=PRODUCTS, collections=COLLECTIONS,
            skip={"product", "collections"},
        )
        assert "call-banner" in out
        assert "product-cards" not in out and "collection-cards" not in out
    finally:
        get_settings.cache_clear()


def test_blocks_present_reports_each_block_separately():
    from blog_pipeline.agents.convert import blocks_present

    assert blocks_present("<p>nothing</p>") == set()
    assert blocks_present('<div class="call-banner">x</div>') == {"call"}
    assert blocks_present(
        '<div class="call-banner">x</div><div class="collection-cards">y</div>'
    ) == {"call", "collections"}


# ── stripping, for re-runs ───────────────────────────────────────
def test_strip_blocks_removes_a_block_with_nested_markup():
    from blog_pipeline.agents.convert import strip_blocks

    card = render_product_row(PRODUCTS, "Guide")
    assert "<div" in card[5:]  # the row really does nest markup
    body = "<p>One.</p>" + card + "<h2>Two</h2><p>Two.</p>"
    assert strip_blocks(body) == "<p>One.</p><h2>Two</h2><p>Two.</p>"


def test_strip_blocks_leaves_the_article_alone(phone):
    out = apply_conversion_blocks(
        body_html=BODY, campaign="Guide", keywords=["vinyl"],
        products=PRODUCTS, collections=COLLECTIONS,
    )
    from blog_pipeline.agents.convert import strip_blocks

    assert strip_blocks(out) == BODY


def test_strip_blocks_is_a_no_op_on_an_untouched_body():
    from blog_pipeline.agents.convert import strip_blocks

    assert strip_blocks(BODY) == BODY


def test_matching_beats_alphabetical_order():
    """The bug this replaced: exact tokens made "ceramics" miss "Ceramic
    Tile", every score tied at zero, and the catalogue's first three won."""
    catalogue = [
        {"title": "Canadian Flooring Casablanca"},
        {"title": "Clearance"},
        {"title": "Ceramic Tile"},
    ]
    assert pick_best(catalogue, ["Which Is Best, Hardwood Or Ceramics?"], count=1) == [
        {"title": "Ceramic Tile"}
    ]


def test_generic_words_do_not_decide_the_match():
    catalogue = [{"title": "Flooring Accessories"}, {"title": "Bamboo Flooring"}]
    assert pick_best(catalogue, ["All You Need To Know: Bamboo Flooring"], count=1) == [
        {"title": "Bamboo Flooring"}
    ]


# ── relevance ────────────────────────────────────────────────────
def test_a_rare_word_outranks_a_common_one():
    """The catalogue is all flooring, so "flooring" says nothing and
    "herringbone" says everything. Flat overlap weighted them the same."""
    catalogue = [
        {"title": "Oak Flooring"}, {"title": "Vinyl Flooring"},
        {"title": "Laminate Flooring"}, {"title": "Herringbone Flooring"},
    ]
    best = pick_best(catalogue, ["Herringbone Flooring Trends"], count=1)
    assert best == [{"title": "Herringbone Flooring"}]


def test_a_specific_match_is_not_diluted_by_a_long_title():
    """Regression: dividing by every word in the article title buried a
    perfect herringbone hit under planks matching "Columbia" via a tag."""
    catalogue = [
        {"title": "Mega Plank SPC Vinyl", "match_text": "Mega Plank SPC Vinyl British Columbia"},
        {"title": "Harbinger Herringbone", "match_text": "Harbinger Herringbone Vinyl"},
    ]
    best = pick_best(
        catalogue, ["Herringbone Flooring Trends in British Columbia: A Modern Design Guide"],
        count=1,
    )
    assert best[0]["title"] == "Harbinger Herringbone"


def test_match_text_is_used_when_the_title_hides_the_category():
    """Product titles lead with the brand, so the category lives in the
    collections the product belongs to."""
    catalogue = [
        {"title": "AquaFix Plank - Aqua", "match_text": "AquaFix Plank - Aqua SPC Vinyl Flooring"},
        {"title": "Casablanca Laminate", "match_text": "Casablanca Laminate Flooring"},
    ]
    best = pick_best(catalogue, ["The Guide to SPC Flooring"], count=1)
    assert best[0]["title"] == "AquaFix Plank - Aqua"


def test_an_unrelated_article_gets_no_cards_rather_than_filler():
    """Padding a row from the top of the catalogue is how an article about
    hardwood ended up advertising Clearance."""
    catalogue = [{"title": "Clearance"}, {"title": "Ceramic Tile"}]
    assert pick_best(catalogue, ["Choosing Underlay for Stairs"], count=3) == []


def test_generic_article_words_do_not_match_products():
    """"Ultimate" in a title matched "ivc Flexitec Ultimate Sheet Vinyl"."""
    catalogue = [
        {"title": "ivc Flexitec Ultimate Sheet Vinyl"},
        {"title": "AquaFix SPC Plank"},
    ]
    best = pick_best(catalogue, ["The Ultimate Guide to SPC Flooring"], count=1)
    assert best[0]["title"] == "AquaFix SPC Plank"


def test_ranking_is_stable_when_scores_tie():
    """A whole range scores identically — eleven herringbone planks differ
    only by colour — and Shopify does not return pages in a stable order, so
    ranking by catalogue position rewrote posts that had not changed."""
    a = [{"title": "Herringbone Oak"}, {"title": "Herringbone Ash"}, {"title": "Herringbone Elm"}]
    assert pick_best(a, ["Herringbone"], 2) == pick_best(list(reversed(a)), ["Herringbone"], 2)
