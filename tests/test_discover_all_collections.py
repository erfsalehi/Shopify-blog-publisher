from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from dashboard import import_queue, manufacturer
from dashboard.db import get_session
from dashboard.models import ImportQueueEntry, ImportQueueStatus


@pytest.fixture
def client(dashboard_db):
    from dashboard.web import create_app

    with TestClient(create_app()) as c:
        yield c


SHOPIFY_COLLECTIONS_JSON = {
    "collections": [
        {
            "id": 101,
            "title": "Advantage",
            "handle": "advantage",
            "products_count": 12,
            "image": {"src": "https://cdn.maker.test/advantage.jpg"},
        },
        {
            "id": 102,
            "title": "Anthology",
            "handle": "anthology",
            "products_count": 8,
            "image": {"src": "https://cdn.maker.test/anthology.jpg"},
        },
        {
            "id": 999,
            "title": "All Products",
            "handle": "all",
            "products_count": 500,
        },
    ]
}

HTML_ALL_COLLECTIONS = """
<!doctype html>
<html>
<head><title>All Collections - Maker</title></head>
<body>
  <h1>Explore Collections</h1>
  <div class="collection-grid">
    <div class="collection-card">
      <a href="/collections/series-one">
        <img src="https://maker.test/images/series-one.jpg" alt="Series One">
        <h2>Series One Collection</h2>
      </a>
    </div>
    <div class="collection-card">
      <a href="/collections/series-two">
        <img src="https://maker.test/images/series-two.jpg" alt="Series Two">
        <h3>Series Two Luxury</h3>
      </a>
    </div>
    <div class="collection-card">
      <a href="/collections/all">
        <span>Shop All</span>
      </a>
    </div>
  </div>
</body>
</html>
"""


def test_discover_all_collections_shopify_json():
    with respx.mock(assert_all_called=False) as router:
        router.get("https://maker.test/robots.txt").respond(200, text="User-agent: *\nAllow: /")
        router.get("https://maker.test/collections.json?limit=250").respond(200, json=SHOPIFY_COLLECTIONS_JSON)

        results = manufacturer.discover_all_collections("https://maker.test/collections")
        assert len(results) == 2  # 'all' handle is filtered out
        assert results[0]["title"] == "Advantage"
        assert results[0]["url"] == "https://maker.test/collections/advantage"
        assert results[0]["image_url"] == "https://cdn.maker.test/advantage.jpg"
        assert results[1]["title"] == "Anthology"
        assert results[1]["url"] == "https://maker.test/collections/anthology"


def test_discover_all_collections_html_scrape():
    with respx.mock(assert_all_called=False) as router:
        router.get("https://htmlmaker.test/robots.txt").respond(200, text="User-agent: *\nAllow: /")
        # collections.json returns 404
        router.get("https://htmlmaker.test/collections.json?limit=250").respond(404)
        router.get("https://htmlmaker.test/categories").respond(200, text=HTML_ALL_COLLECTIONS)

        results = manufacturer.discover_all_collections("https://htmlmaker.test/categories")
        assert len(results) == 2
        titles = [r["title"] for r in results]
        assert "Series One Collection" in titles
        assert "Series Two Luxury" in titles
        urls = [r["url"] for r in results]
        assert "https://htmlmaker.test/collections/series-one" in urls
        assert "https://htmlmaker.test/collections/series-two" in urls


def test_web_route_discover_and_queue_selected(client):
    with respx.mock(assert_all_called=False) as router:
        router.get("https://maker.test/robots.txt").respond(200, text="User-agent: *\nAllow: /")
        router.get("https://maker.test/collections.json?limit=250").respond(200, json=SHOPIFY_COLLECTIONS_JSON)

        # 1. Discover collections
        resp = client.post(
            "/import/discover-collections",
            data={"collections_url": "https://maker.test/collections", "vendor": "Ames Tile"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Choose collections to import" in resp.text
        assert "Advantage" in resp.text
        assert "Anthology" in resp.text

    # 2. Submit selected collections
    resp = client.post(
        "/import/queue-selected",
        data={
            "vendor": "Ames Tile",
            "dry_run": "1",
            "selected_urls": [
                "https://maker.test/collections/advantage",
                "https://maker.test/collections/anthology",
            ],
            "title_https://maker.test/collections/advantage": "Advantage Porcelain",
            "title_https://maker.test/collections/anthology": "Anthology Collection",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "notice=" in resp.headers["location"]

    # Verify they landed in the import queue
    queue = import_queue.entries()
    assert len(queue) == 2
    assert queue[0]["source_url"] == "https://maker.test/collections/advantage"
    assert queue[0]["collection_title"] == "Advantage Porcelain"
    assert queue[0]["vendor"] == "Ames Tile"
    assert queue[0]["dry_run"] is True
    assert queue[1]["source_url"] == "https://maker.test/collections/anthology"
    assert queue[1]["collection_title"] == "Anthology Collection"
