"""Finding products on a store that doesn't prefix their URLs.

Built from a real failure: importing amestile.com's "3D Bars" collection
found nothing and reported "the page loaded, but nothing on it looked like a
product link — if the collection renders its grid with JavaScript, this
can't see it". The grid was plain server-rendered HTML sitting in the
response, and every product was in it.

The store is Magento, which puts products at the site root — `/3dbbemg510`,
not `/products/3dbbemg510`. `_product_links` can only recognise a product by
its path, and no pattern that matches a bare slug could avoid also matching
`/about`. So the shape of the URL is the wrong question to ask, and the grid
is asked instead.

The markup below is trimmed from that page, keeping the parts that made it
fail: bare product URLs, wishlist and compare anchors that are links too, a
catalogue download that looks exactly like a product URL and isn't, and
Magento's `?p=` paging.
"""

from __future__ import annotations

import httpx
import pytest

from dashboard import manufacturer

BASE = "https://magento.test"
PATH = "/collections/3dbars"

#: One card, as Magento renders it. Three anchors, one of which is the
#: product: the other two are the wishlist and compare controls, which point
#: back at the collection with a fragment on the end.
CARD = """
<li class="item product product-item">
  <div class="product-item-info" data-container="product-grid">
    <a href="{url}" class="product photo product-item-photo">
      <img src="/media/{slug}.jpg" alt="{slug}">
    </a>
    <div class="addto-button">
      <a href="/collections/3dbars#" class="action towishlist">Add to Favorites</a>
      <a href="/collections/3dbars#" class="action tocompare">Add to Compare</a>
    </div>
  </div>
</li>
"""

PAGE_ONE = """
<html><body>
  <h1 class="page-title"><span class="base">3D Bars</span></h1>
  <div class="category-description">
    <a href="/3dbarscatalogue" class="action tocart primary">Digital Magazine</a>
  </div>
  <p class="toolbar-amount">Items 1-2 of 3</p>
  <div class="pages">
    <li class="item"><a href="/collections/3dbars?p=2" class="page">2</a></li>
    <li class="item pages-item-next"><a class="action next" href="/collections/3dbars?p=2">Next</a></li>
  </div>
  <div class="products wrapper grid products-grid">
    <ol class="products list items product-items">
""" + CARD.format(url="/3dbbemg510", slug="3dbbemg510") \
    + CARD.format(url="/3dbdemg510", slug="3dbdemg510") + """
    </ol>
  </div>
</body></html>
"""

PAGE_TWO = """
<html><body>
  <h1 class="page-title"><span class="base">3D Bars</span></h1>
  <div class="products wrapper grid products-grid">
    <ol class="products list items product-items">
""" + CARD.format(url="/3dbbsphg510", slug="3dbbsphg510") + """
    </ol>
  </div>
</body></html>
"""

PRODUCT_PAGE = """
<html><body><h1>A tile</h1>
<div class="product-info-main"><span class="price">$9.99</span></div>
</body></html>
"""


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/robots.txt":
        return httpx.Response(200, text="")
    if path.endswith("products.json"):
        return httpx.Response(404)          # not Shopify
    if path == PATH:
        # Magento ignores an unknown paging parameter and serves page one.
        # This is the behaviour that made the old code stop at page one
        # while believing it had reached the end.
        if request.url.params.get("p") == "2":
            return httpx.Response(200, html=PAGE_TWO)
        return httpx.Response(200, html=PAGE_ONE)
    return httpx.Response(200, html=PRODUCT_PAGE)


@pytest.fixture
def magento(monkeypatch):
    from dashboard.competitors import reset_robots_cache

    transport = httpx.MockTransport(_handler)
    monkeypatch.setattr(
        manufacturer, "client",
        lambda: httpx.Client(transport=transport, follow_redirects=True, timeout=5.0),
    )
    monkeypatch.setattr(manufacturer, "PAUSE", 0.0)
    reset_robots_cache()


def _no_firecrawl(monkeypatch):
    """Assert the render fallback is never reached.

    It costs a credit and, on this page, would return the same markup the
    plain GET already had. Reaching for it here would mean the grid parse
    quietly failed and the test passed for the wrong reason.
    """
    def explode(url):
        raise AssertionError(f"Firecrawl should not be needed for {url}")

    monkeypatch.setattr(manufacturer, "_firecrawl_scrape", explode)


def test_products_at_the_site_root_are_found(magento, monkeypatch):
    """The original failure: every product URL is a bare slug."""
    _no_firecrawl(monkeypatch)
    found = manufacturer.discover_collection(f"{BASE}{PATH}")
    assert f"{BASE}/3dbbemg510" in found.product_urls
    assert f"{BASE}/3dbdemg510" in found.product_urls


def test_the_catalogue_download_is_not_imported_as_a_product(magento, monkeypatch):
    """`/3dbarscatalogue` is indistinguishable from a product by its URL
    alone — only its position outside the grid says otherwise. This is what
    a looser URL pattern would have got wrong."""
    _no_firecrawl(monkeypatch)
    found = manufacturer.discover_collection(f"{BASE}{PATH}")
    assert f"{BASE}/3dbarscatalogue" not in found.product_urls


def test_wishlist_and_compare_links_are_not_products(magento, monkeypatch):
    """Both are anchors inside the card. Stripped of their fragment they are
    the collection page itself."""
    _no_firecrawl(monkeypatch)
    found = manufacturer.discover_collection(f"{BASE}{PATH}")
    assert all(not u.rstrip("/").endswith("3dbars") for u in found.product_urls)


def test_paging_follows_the_parameter_the_page_actually_uses(magento, monkeypatch):
    """Magento answers `?page=2` with HTTP 200 and page one, so guessing
    wrong is silent: the caller sees a good fetch naming no new products and
    concludes the collection ended one page early."""
    _no_firecrawl(monkeypatch)
    found = manufacturer.discover_collection(f"{BASE}{PATH}")
    assert f"{BASE}/3dbbsphg510" in found.product_urls
    assert len(found.product_urls) == 3


def test_the_collection_title_still_comes_from_the_page(magento, monkeypatch):
    _no_firecrawl(monkeypatch)
    assert manufacturer.discover_collection(f"{BASE}{PATH}").title == "3D Bars"


def test_a_page_with_no_grid_still_falls_back_to_the_url_pattern(monkeypatch):
    """The grid is extra evidence, not a replacement. A store that prefixes
    its product URLs and marks up no cards must keep working."""
    from dashboard.competitors import reset_robots_cache

    plain = """
    <html><body><h1>Tiles</h1>
      <a href="/products/oak-12mm">Oak</a>
      <a href="/about">About us</a>
    </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if request.url.path.endswith("products.json"):
            return httpx.Response(404)
        return httpx.Response(200, html=plain)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        manufacturer, "client",
        lambda: httpx.Client(transport=transport, follow_redirects=True, timeout=5.0),
    )
    monkeypatch.setattr(manufacturer, "PAUSE", 0.0)
    reset_robots_cache()

    found = manufacturer.discover_collection(f"{BASE}{PATH}")
    assert found.product_urls == [f"{BASE}/products/oak-12mm"]
