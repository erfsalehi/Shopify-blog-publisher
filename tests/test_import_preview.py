"""Reading a dry run's copy before deciding to publish it.

A dry run does the whole expensive path — scrape, read the documents, call
the model — and stops short of the create. Until this page existed, the only
thing it showed of that work was a title, an SEO title and some counts,
which cannot answer the question a dry run is for: is this good enough to
put in the store?

So the test that matters is that the preview shows the *same body the real
create would send*, not a summary of it. Everything else here is about the
two places a preview is honestly different from the real thing.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from dashboard import product_copy, product_import
from dashboard.db import get_session
from dashboard.models import ImportProduct, ImportProductStatus, ImportRun, ImportStage

COPY = {
    "title": "3D Bars Emerald Bevel Gloss 5x10",
    "product_type": "Wall Tile",
    "summary": "A glossy emerald bevel tile that shifts with the light.",
    "paragraphs": ["Fired in Spain and finished by hand.", "Suits a feature wall."],
    "features": ["Gloss finish", "Bevelled face"],
    "specs": [{"name": "Size", "value": "5in x 10in"}],
    "applications": ["Feature walls", "Backsplashes"],
    "faqs": [{"question": "Is it frost proof?", "answer": "No. Interior use only."}],
    "tags": ["emerald", "gloss", "wall tile"],
    "seo_title": "3D Bars Emerald Bevel Gloss | D&R Flooring",
    "seo_description": "Emerald bevel gloss wall tile, 5x10, in stock in Langley.",
    "model": "gemini-3-flash-preview",
}

EXTRACTED = {
    "source_url": "https://www.amestile.com/3dbbemg510",
    "images": [
        {"url": "https://www.amestile.com/media/3dbbemg510.jpg",
         "alt": "Emerald bevel gloss", "position": 1},
    ],
    "specs": {"Material": "Ceramic"},
    "docs": [
        {"url": "https://www.amestile.com/sheet.pdf", "title": "Product sheet",
         "kind": "spec_sheet", "pages": 4, "text": "x" * 900},
        {"url": "https://www.amestile.com/broken.pdf", "title": "Installation",
         "kind": "manual", "error": "HTTP 404"},
    ],
}


@pytest.fixture
def dry_run(dashboard_db):
    """A finished dry run with one prepared product, as the app would leave it."""
    with get_session() as session:
        run = ImportRun(
            source_url="https://www.amestile.com/collections/3dbars",
            source_base="https://www.amestile.com",
            collection_title="3D Bars",
            dry_run=True,
            stage=ImportStage.done.value,
            options_json=json.dumps({"source_tag": "imported"}),
        )
        session.add(run)
        session.flush()
        row = ImportProduct(
            run_id=run.id, position=1,
            source_url=EXTRACTED["source_url"],
            title=COPY["title"],
            status=ImportProductStatus.prepared.value,
            extracted_json=json.dumps(EXTRACTED),
            generated_json=json.dumps(COPY),
        )
        session.add(row)
        session.flush()
        return {"run_id": run.id, "product_id": row.id}


@pytest.fixture
def client(dashboard_db):
    from dashboard.web import create_app

    with TestClient(create_app()) as c:
        yield c


# ── The claim the page makes ───────────────────────────────────────


def test_the_preview_is_the_body_the_real_create_would_send(dry_run):
    """Not "close to" — the same string, from the same function. A preview
    assembled separately would drift from the create the first time either
    changed, and drift is invisible until someone publishes."""
    preview = product_import.product_preview(dry_run["run_id"], dry_run["product_id"])
    expected = product_copy.render_description(
        product_copy.ProductCopy.model_validate(
            {k: v for k, v in COPY.items() if k != "model"}
        ),
        docs=preview["docs"], doc_urls={},
        source_url=EXTRACTED["source_url"],
        locale=product_copy.locale_text(),
    )
    assert preview["body_html"] == expected
    assert "shifts with the light" in preview["body_html"]
    assert "Is it frost proof?" in preview["body_html"]


def test_the_source_tag_is_shown_with_the_models_own_tags(dry_run):
    """The store gets `imported` added at create time. A preview that showed
    only the model's tags would misrepresent what lands."""
    preview = product_import.product_preview(dry_run["run_id"], dry_run["product_id"])
    assert "imported" in preview["tags"]
    assert "emerald" in preview["tags"]


def test_downloads_are_absent_because_a_dry_run_uploads_nothing(dry_run):
    """`render_downloads` drops a document with no store URL rather than
    linking the manufacturer's — a broken promise of a download being worse
    than none. The preview inherits that, and lists the documents
    separately instead."""
    preview = product_import.product_preview(dry_run["run_id"], dry_run["product_id"])
    assert "Downloads" not in preview["body_html"]
    assert len(preview["docs"]) == 2


def test_a_document_that_failed_to_read_is_visible(dry_run):
    """A thin description is either a thin source or a PDF that didn't load,
    and those have different fixes."""
    preview = product_import.product_preview(dry_run["run_id"], dry_run["product_id"])
    failed = [d for d in preview["docs"] if d.error]
    assert len(failed) == 1
    assert failed[0].error == "HTTP 404"


# ── Not falling over ───────────────────────────────────────────────


def test_a_product_with_no_copy_yet_previews_without_crashing(dashboard_db):
    with get_session() as session:
        run = ImportRun(source_url="https://x.test/c", stage=ImportStage.products.value)
        session.add(run)
        session.flush()
        row = ImportProduct(run_id=run.id, position=1, source_url="https://x.test/p")
        session.add(row)
        session.flush()
        ids = (run.id, row.id)
    preview = product_import.product_preview(*ids)
    assert preview["copy"] is None
    assert preview["body_html"] == ""


def test_stored_copy_that_no_longer_fits_the_schema_does_not_500(dry_run):
    """`ProductCopy` will change. A row written by an older version must
    degrade to "nothing to preview", not take the page down."""
    with get_session() as session:
        row = session.get(ImportProduct, dry_run["product_id"])
        row.generated_json = json.dumps({"summary": "no title field at all"})
    preview = product_import.product_preview(dry_run["run_id"], dry_run["product_id"])
    assert preview["copy"] is None
    assert preview["body_html"] == ""


def test_a_product_from_another_run_is_refused(dry_run):
    """The run id in the URL must actually own the product, or one run's
    page could show another's copy."""
    with pytest.raises(product_import.ImportRunError):
        product_import.product_preview(dry_run["run_id"] + 999, dry_run["product_id"])


# ── The page ───────────────────────────────────────────────────────


def test_the_page_renders_the_copy_and_what_produced_it(client, dry_run):
    body = client.get(
        f"/import/{dry_run['run_id']}/product/{dry_run['product_id']}"
    ).text
    assert "shifts with the light" in body          # the description
    assert "Is it frost proof?" in body             # the FAQ
    assert "D&amp;R Flooring" in body               # the SEO title, escaped
    assert "HTTP 404" in body                       # the document that failed
    assert "gemini-3-flash-preview" in body         # which model wrote it


def test_the_run_page_links_each_product_to_its_preview(client, dry_run):
    body = client.get(f"/import/{dry_run['run_id']}").text
    assert f"/import/{dry_run['run_id']}/product/{dry_run['product_id']}" in body


def test_an_unknown_product_redirects_rather_than_500s(client, dry_run):
    response = client.get(
        f"/import/{dry_run['run_id']}/product/999999", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/import/{dry_run['run_id']}")
