"""Photos a page hands to JavaScript, and the store's own block.

Both come from the same import: Amestile's product pages carry two
photographs each and `collect_images` found zero of them, because Magento
leaves the markup with a placeholder div and passes the real gallery to
JavaScript as JSON. Scanning `<img>` can never see that.

The brand block is the other half — what the store stocks, what's on offer,
and to call. It lives in settings rather than in code because it is the part
of a product page most likely to be wrong tomorrow, and a threshold written
into the app would sit stale across a published catalogue.
"""

from __future__ import annotations

import json

from dashboard import product_copy, store
from dashboard.manufacturer import collect_images, soup_of

BASE = "https://www.amestile.com"
CACHE = BASE + "/media/catalog/product/cache"

#: Trimmed from the real page. Three renditions of each of two photographs,
#: each rendition under a different cache hash — the size is a directory
#: here, not a filename suffix.
GALLERY = {
    "[data-gallery-role=gallery-placeholder]": {
        "mage/gallery/gallery": {
            "data": [
                {
                    "thumb": f"{CACHE}/04ad/3/d/tile.jpg",
                    "img": f"{CACHE}/1790/3/d/tile.jpg",
                    "full": f"{CACHE}/44db/3/d/tile.jpg",
                    "caption": 'Emerald Bevel Gloss 5"x10"',
                    "position": "1", "isMain": True, "type": "image",
                    "videoUrl": None,
                },
                {
                    "thumb": f"{CACHE}/04ad/3/d/roomscene.jpg",
                    "img": f"{CACHE}/1790/3/d/roomscene.jpg",
                    "full": f"{CACHE}/44db/3/d/roomscene.jpg",
                    "caption": "Roomscene",
                    "position": "2", "isMain": False, "type": "image",
                    "videoUrl": None,
                },
            ]
        }
    }
}


def _page(gallery: dict, body: str = "") -> str:
    return (
        "<html><body>"
        + body
        + '<script type="text/x-magento-init">'
        + json.dumps(gallery)
        + "</script></body></html>"
    )


# ── The gallery ────────────────────────────────────────────────────


def test_both_photographs_are_found(dashboard_db):
    """The reported symptom: two pictures on the page, one (in fact none)
    imported."""
    images = collect_images(soup_of(_page(GALLERY)), BASE)
    assert len(images) == 2


def test_the_full_size_rendition_is_the_one_taken(dashboard_db):
    """Importing the thumbnail is the failure that looks fine until someone
    zooms. The entry says which rendition is which; a regex over URLs would
    throw that away."""
    images = collect_images(soup_of(_page(GALLERY)), BASE)
    assert all("/44db/" in image.url for image in images)


def test_three_renditions_of_one_photo_are_not_three_photos(dashboard_db):
    """`_normalize_image_url` strips size *suffixes*, and Magento's size is a
    directory, so the three URLs for one photo look unrelated to it."""
    images = collect_images(soup_of(_page(GALLERY)), BASE)
    names = [image.url.rsplit("/", 1)[-1] for image in images]
    assert sorted(names) == ["roomscene.jpg", "tile.jpg"]


def test_the_caption_becomes_the_alt_text(dashboard_db):
    images = collect_images(soup_of(_page(GALLERY)), BASE)
    assert images[0].alt == 'Emerald Bevel Gloss 5"x10"'
    assert images[1].alt == "Roomscene"


def test_a_video_entry_is_not_imported_as_a_photograph(dashboard_db):
    """Its poster frame is a real image URL. Importing it puts a play button
    on the product."""
    gallery = json.loads(json.dumps(GALLERY))
    entries = gallery["[data-gallery-role=gallery-placeholder]"]["mage/gallery/gallery"]["data"]
    entries.append({
        "thumb": f"{CACHE}/04ad/3/d/poster.jpg",
        "full": f"{CACHE}/44db/3/d/poster.jpg",
        "caption": "How it installs", "type": "video",
        "videoUrl": "https://youtube.com/watch?v=x",
    })
    images = collect_images(soup_of(_page(gallery)), BASE)
    assert all("poster" not in image.url for image in images)
    assert len(images) == 2


def test_the_gallery_wins_over_the_pages_furniture(dashboard_db):
    """When the site states which photographs are the product's, scanning
    `<img>` as well would only add the header logo back in."""
    body = '<img src="/media/header-banner.jpg" alt="banner">'
    images = collect_images(soup_of(_page(GALLERY, body)), BASE)
    assert all("banner" not in image.url for image in images)
    assert len(images) == 2


def test_a_page_with_no_gallery_still_scans_img_tags(dashboard_db):
    """The gallery is extra evidence, not a replacement. A plain storefront
    must keep working."""
    html = '<html><body><img src="/media/oak.jpg" alt="Oak"></body></html>'
    images = collect_images(soup_of(html), BASE)
    assert [i.url for i in images] == [f"{BASE}/media/oak.jpg"]


def test_a_script_that_is_not_json_does_not_break_the_scrape(dashboard_db):
    html = (
        '<html><body><script type="text/x-magento-init">'
        '{"thumb": not json at all,,,'
        '</script><img src="/media/oak.jpg"></body></html>'
    )
    assert [i.url for i in collect_images(soup_of(html), BASE)] == [
        f"{BASE}/media/oak.jpg"
    ]


# ── The brand block ────────────────────────────────────────────────


def test_the_block_is_added_to_every_description(dashboard_db):
    copy = product_copy.ProductCopy(
        title="Oak", product_type="Flooring", summary="Oak flooring.",
        seo_title="Oak flooring", seo_description="Oak flooring in Langley.",
    )
    body = product_copy.render_description(
        copy, brand=product_copy.brand_blurb_text()
    )
    assert "Langley Bypass" in body
    assert "product-brand" in body


def test_the_offer_is_editable_without_a_deploy(dashboard_db):
    """The whole reason it is a setting. A delivery threshold written into
    the app would sit stale on a catalogue that is already published."""
    store.set(store.IMPORT_BRAND_BLURB, "Spend $3,000 for free delivery.")
    assert "$3,000" in product_copy.render_brand_blurb(
        product_copy.brand_blurb_text()
    )


def test_the_shipped_default_carries_no_stale_date(dashboard_db):
    """The text this was seeded from said "In March 2024". Publishing a date
    that old across the catalogue is worse than publishing no date."""
    assert "2024" not in product_copy.brand_blurb_text()


def test_both_delivery_tiers_and_their_cities_survive(dashboard_db):
    body = product_copy.render_brand_blurb(product_copy.brand_blurb_text())
    assert "$2,000" in body and "$7,000" in body
    for city in ("Surrey", "Coquitlam", "Whistler", "Abbotsford", "Mission"):
        assert city in body


def test_the_store_address_becomes_a_link(dashboard_db):
    body = product_copy.render_brand_blurb("Call us: https://www.drflooring.ca")
    assert '<a href="https://www.drflooring.ca"' in body


def test_html_in_the_setting_is_escaped_not_rendered(dashboard_db):
    """One clickable link is worth having; a settings field that can put
    arbitrary markup on every product in the store is not."""
    body = product_copy.render_brand_blurb(
        "Sale <script>alert(1)</script> & more"
    )
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert "&amp; more" in body


def test_blank_lines_become_paragraphs(dashboard_db):
    body = product_copy.render_brand_blurb("First para.\n\nSecond para.")
    assert body.count("<p>") == 2


def test_an_empty_setting_adds_nothing(dashboard_db):
    """Turning it off must leave no empty div behind."""
    store.set(store.IMPORT_BRAND_BLURB, "")
    copy = product_copy.ProductCopy(
        title="Oak", product_type="Flooring", summary="Oak flooring.",
        seo_title="Oak flooring", seo_description="Oak flooring in Langley.",
    )
    body = product_copy.render_description(
        copy, brand=product_copy.brand_blurb_text()
    )
    assert "product-brand" not in body


def test_rendering_needs_no_database(dashboard_db):
    """`render_description` is pure. It reached into settings for one commit
    and broke every test that rendered copy without a database."""
    assert product_copy.render_brand_blurb("Call us.") == (
        '<div class="product-brand"><p>Call us.</p></div>'
    )
