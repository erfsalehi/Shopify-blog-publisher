"""Keeping the picture the range page was already showing.

Two symptoms, one cause. Products arrived with only one photograph when
their page carries two, and some arrived with none at all — while the
collection page we had just read was showing a thumbnail for every one of
them.

The thumbnail was being read (it is inside the anchor used to find the
product) and then thrown away, because seeds only ever came from Shopify's
`products.json`. Nothing carried a non-Shopify grid's picture forward.

Fixing that alone would have caused the other symptom: `_merge_page` filled
images *only when the product had none*, so a seeded thumbnail would have
suppressed the product page's own gallery and left every product with
exactly one picture — the small one. Structured images from the page now
extend the seed instead of being skipped.
"""

from __future__ import annotations

import json

import httpx
import pytest

from dashboard import manufacturer

BASE = "https://magento.test"
PATH = "/collections/3dbars"

CARD = """
<li class="item product product-item">
  <div class="product-item-info" data-container="product-grid">
    <a href="{url}" class="product photo product-item-photo">
      <img class="product-image-photo" src="/media/cache/thumb/{slug}.jpg"
           alt="3D Bars | 5&quot;x10&quot; {name}">
    </a>
    <a href="/collections/3dbars#" class="action towishlist">Add to Favorites</a>
  </div>
</li>
"""

GRID = (
    '<html><body><h1 class="page-title"><span class="base">3D Bars</span></h1>'
    '<div class="products wrapper grid products-grid"><ol class="product-items">'
    + CARD.format(url="/3dbbemg510", slug="3dbbemg510", name="Emerald Bevel Gloss")
    + CARD.format(url="/3dbdjdg510", slug="3dbdjdg510", name="Jade Diamond Gloss")
    + "</ol></div></body></html>"
)

#: The product page for the first one: a real gallery, two photographs.
GALLERY_PAGE = (
    '<html><body><h1>Emerald Bevel Gloss</h1>'
    '<script type="text/x-magento-init">'
    + json.dumps({"[data-gallery-role=gallery-placeholder]": {
        "mage/gallery/gallery": {"data": [
            {"thumb": f"{BASE}/media/cache/04ad/3dbbemg510.jpg",
             "img": f"{BASE}/media/cache/1790/3dbbemg510.jpg",
             "full": f"{BASE}/media/cache/44db/3dbbemg510.jpg",
             "caption": "Emerald Bevel Gloss", "type": "image",
             "videoUrl": None},
            {"thumb": f"{BASE}/media/cache/04ad/roomscene.jpg",
             "img": f"{BASE}/media/cache/1790/roomscene.jpg",
             "full": f"{BASE}/media/cache/44db/roomscene.jpg",
             "caption": "Roomscene", "type": "image", "videoUrl": None},
        ]}}})
    + "</script></body></html>"
)

#: The second one's page has no gallery and no usable photograph at all —
#: the case that used to produce a product with no image.
BARE_PAGE = '<html><body><h1>Jade Diamond Gloss</h1><p>A tile.</p></body></html>'


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/robots.txt":
        return httpx.Response(200, text="")
    if path.endswith("products.json"):
        return httpx.Response(404)
    if path == PATH:
        return httpx.Response(200, html=GRID)
    if path == "/3dbbemg510":
        return httpx.Response(200, html=GALLERY_PAGE)
    return httpx.Response(200, html=BARE_PAGE)


@pytest.fixture
def site(monkeypatch):
    from dashboard.competitors import reset_robots_cache

    transport = httpx.MockTransport(_handler)
    monkeypatch.setattr(
        manufacturer, "client",
        lambda: httpx.Client(transport=transport, follow_redirects=True, timeout=5.0),
    )
    monkeypatch.setattr(manufacturer, "PAUSE", 0.0)
    monkeypatch.setattr(manufacturer, "_firecrawl_scrape", lambda url: (None, None))
    reset_robots_cache()
    return transport


def _client(transport):
    return httpx.Client(transport=transport, follow_redirects=True, timeout=5.0)


# ── The picture the range page had all along ───────────────────────


def test_the_grid_thumbnail_is_kept_as_a_seed(site, dashboard_db):
    found = manufacturer.discover_collection(f"{BASE}{PATH}")
    seed = found.seeds[f"{BASE}/3dbbemg510"]
    assert seed.images and "3dbbemg510.jpg" in seed.images[0].url


def test_a_product_page_with_no_photograph_still_gets_a_cover(
    site, dashboard_db
):
    """The reported symptom. Its thumbnail was one click away in the
    collection we had just read."""
    found = manufacturer.discover_collection(f"{BASE}{PATH}")
    seed = found.seeds[f"{BASE}/3dbdjdg510"]
    with _client(site) as http:
        product = manufacturer.fetch_product(
            f"{BASE}/3dbdjdg510", BASE, seed=seed, http=http
        )
    assert len(product.images) == 1
    assert "3dbdjdg510.jpg" in product.images[0].url


def test_the_grid_alt_text_becomes_the_seed_title(site, dashboard_db):
    found = manufacturer.discover_collection(f"{BASE}{PATH}")
    assert "Emerald Bevel Gloss" in found.seeds[f"{BASE}/3dbbemg510"].title


# ── And the page's own gallery on top of it ────────────────────────


def test_a_seeded_thumbnail_does_not_suppress_the_gallery(site, dashboard_db):
    """The trap in fixing the cover. Images used to fill only when the
    product had none, so one seeded thumbnail would have replaced two
    full-size photographs with one small one."""
    found = manufacturer.discover_collection(f"{BASE}{PATH}")
    seed = found.seeds[f"{BASE}/3dbbemg510"]
    with _client(site) as http:
        product = manufacturer.fetch_product(
            f"{BASE}/3dbbemg510", BASE, seed=seed, http=http
        )
    urls = " ".join(i.url for i in product.images)
    assert "3dbbemg510.jpg" in urls and "roomscene.jpg" in urls


def test_the_full_size_renditions_come_through_not_the_thumbnails(
    site, dashboard_db
):
    found = manufacturer.discover_collection(f"{BASE}{PATH}")
    seed = found.seeds[f"{BASE}/3dbbemg510"]
    with _client(site) as http:
        product = manufacturer.fetch_product(
            f"{BASE}/3dbbemg510", BASE, seed=seed, http=http
        )
    assert any("/44db/roomscene.jpg" in i.url for i in product.images)


def test_positions_are_renumbered_across_the_merged_set(site, dashboard_db):
    """Shopify orders media by position, and a seed at 1 plus a gallery
    starting again at 1 would put two images in the same slot."""
    found = manufacturer.discover_collection(f"{BASE}{PATH}")
    seed = found.seeds[f"{BASE}/3dbbemg510"]
    with _client(site) as http:
        product = manufacturer.fetch_product(
            f"{BASE}/3dbbemg510", BASE, seed=seed, http=http
        )
    positions = [i.position for i in product.images]
    assert positions == list(range(1, len(positions) + 1))


def test_the_same_photo_from_both_sources_is_not_imported_twice(
    site, dashboard_db
):
    """The grid thumbnail and the gallery's thumb rendition are the same
    photograph under different cache paths."""
    found = manufacturer.discover_collection(f"{BASE}{PATH}")
    seed = found.seeds[f"{BASE}/3dbbemg510"]
    with _client(site) as http:
        product = manufacturer.fetch_product(
            f"{BASE}/3dbbemg510", BASE, seed=seed, http=http
        )
    names = [i.url.rsplit("/", 1)[-1] for i in product.images]
    assert names.count("3dbbemg510.jpg") == 1
    assert len(names) == len(set(names)) == 2
