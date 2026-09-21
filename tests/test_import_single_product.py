from __future__ import annotations

import json
import pytest
from fastapi.testclient import TestClient

from dashboard import product_import
from dashboard.db import get_session
from dashboard.models import ImportProduct, ImportProductStatus, ImportRun, ImportStage
from tests.test_product_import import (
    fake_shopify,
    fake_site,
    no_llm,
)


@pytest.fixture
def client(dashboard_db):
    from dashboard.web import create_app

    with TestClient(create_app()) as c:
        yield c


def test_start_run_single_product_creates_run(dashboard_db):
    run_id = product_import.start_run(
        "https://maker.test/products/sample-tile",
        vendor="Ames Tile",
        collection_title="Pure Plank",
        is_single_product=True,
        dry_run=True,
    )
    with get_session() as session:
        run = session.get(ImportRun, run_id)
        assert run is not None
        assert run.options.get("single_product") is True
        assert run.vendor == "Ames Tile"
        assert run.collection_title == "Pure Plank"
        assert run.stage == ImportStage.discover.value


def test_single_product_discover_stage(dashboard_db, fake_site):
    run_id = product_import.start_run(
        "https://maker.test/products/3d-bars-white",
        vendor="Ames Tile",
        collection_title="3D Bars",
        is_single_product=True,
        dry_run=True,
    )
    result = product_import.advance(run_id)
    assert result.stage == ImportStage.products.value
    assert result.handled == 1

    with get_session() as session:
        run = session.get(ImportRun, run_id)
        assert run.stage == ImportStage.products.value
        assert run.collection_title == "3D Bars"

        products = session.query(ImportProduct).filter(ImportProduct.run_id == run_id).all()
        assert len(products) == 1
        assert products[0].source_url == "https://maker.test/products/3d-bars-white"
        assert products[0].status == ImportProductStatus.pending.value


def test_single_product_end_to_end_dry_run(dashboard_db, fake_site, no_llm):
    run_id = product_import.start_run(
        "https://maker.test/products/3d-bars-white",
        vendor="Ames Tile",
        collection_title="3D Bars",
        is_single_product=True,
        dry_run=True,
    )
    # Advance through all stages until done
    for _ in range(10):
        r = product_import.advance(run_id)
        if r.done:
            break

    assert r.stage == ImportStage.done.value
    assert r.done is True

    with get_session() as session:
        run = session.get(ImportRun, run_id)
        assert run.stage == ImportStage.done.value
        product = session.query(ImportProduct).filter(ImportProduct.run_id == run_id).first()
        assert product.status == ImportProductStatus.prepared.value
        assert product.title is not None


def test_web_route_single_product_validation(client):
    # Missing vendor
    resp = client.post(
        "/import/product",
        data={"source_url": "https://maker.test/products/sample", "vendor": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]

    # Missing source_url
    resp = client.post(
        "/import/product",
        data={"source_url": "", "vendor": "Ames Tile"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]

    # Rejects collection URL in single product importer
    resp = client.post(
        "/import/product",
        data={
            "source_url": "https://www.amestile.com/collections/beraberen",
            "vendor": "Ames Tile",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]
    assert "collection+URL" in resp.headers["location"] or "collection" in resp.headers["location"]

    # Rejects single product URL in collection importer
    resp = client.post(
        "/import",
        data={
            "source_url": "https://www.amestile.com/products/single-tile",
            "vendor": "Ames Tile",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]
    assert "product+URL" in resp.headers["location"] or "product" in resp.headers["location"]

    # Valid submission
    resp = client.post(
        "/import/product",
        data={
            "source_url": "https://maker.test/products/sample",
            "vendor": "Ames Tile",
            "collection_title": "Pure Plank",
            "dry_run": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/import/")

