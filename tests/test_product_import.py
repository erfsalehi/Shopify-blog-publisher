"""The product importer: extraction, documents, copy, and a whole run.

The end-to-end test is the important one. It stands up a fake manufacturer
site (a Shopify-shaped collection feed, product pages with spec tables and
PDF links) and a fake Shopify Admin API, then drives a real run through all
four stages and asserts what landed: draft products, images, uploaded
documents, a collection, and every product linking to its siblings.

That shape is deliberate. Each piece here is individually simple; what breaks
in practice is the seam between them — a stage that doesn't hand over what
the next one expects, a resumed run that re-does or skips work. Only a run
catches those.
"""

from __future__ import annotations

import json

import httpx
import pytest

from dashboard import manufacturer, product_copy, product_docs, product_import
from dashboard.db import get_session
from dashboard.models import ImportProduct, ImportProductStatus, ImportRun, ImportStage


# ── A fake manufacturer site ─────────────────────────────────────────

COLLECTION_FEED = {
    "products": [
        {
            "id": 1,
            "handle": "3d-bars-white",
            "title": "3D Bars White",
            "body_html": "<p>Textured ceramic wall tile.</p>",
            "vendor": "Ames Tile",
            "product_type": "Wall Tile",
            "tags": ["ceramic", "wall"],
            "images": [{"src": "https://cdn.maker.test/white.jpg", "alt": "White tile"}],
            "options": [{"name": "Size", "values": ["12x24"]}],
            "variants": [{"sku": "AM-3DB-W"}],
        },
        {
            "id": 2,
            "handle": "3d-bars-black",
            "title": "3D Bars Black",
            "body_html": "<p>Textured ceramic wall tile in black.</p>",
            "vendor": "Ames Tile",
            "product_type": "Wall Tile",
            "tags": ["ceramic", "wall"],
            "images": [{"src": "https://cdn.maker.test/black.jpg", "alt": "Black tile"}],
            "options": [{"name": "Size", "values": ["12x24"]}],
            "variants": [{"sku": "AM-3DB-B"}],
        },
    ]
}

PRODUCT_PAGE = """
<html><head>
  <meta name="description" content="3D Bars textured wall tile.">
</head><body>
  <h1>{title}</h1>
  <table>
    <tr><th>Material</th><td>Ceramic</td></tr>
    <tr><th>Finish</th><td>Matte</td></tr>
  </table>
  <a href="/files/{handle}-spec.pdf">Specification Sheet</a>
  <a href="/files/warranty.pdf">Warranty</a>
  <img src="/cdn/logo.png" alt="logo">
  <img src="/cdn/{handle}-room.jpg" alt="Room scene">
</body></html>
"""

COLLECTION_PAGE = """
<html><head><meta name="description" content="The 3D Bars range."></head>
<body><h1>3D Bars</h1>
<a href="/products/3d-bars-white">White</a>
<a href="/products/3d-bars-black">Black</a>
</body></html>
"""


def _pdf_bytes(text: str = "Wear layer 20 mil") -> bytes:
    """A real PDF with a real text layer, written by hand.

    Hand-built rather than produced by a library so the test exercises the
    extraction path that matters — a page with a font resource and a content
    stream, which is what a manufacturer's spec sheet is. A blank page from a
    PDF writer has no text layer and would only ever prove the scan branch.
    """
    content = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
        + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def maker_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/robots.txt":
        return httpx.Response(200, text="User-agent: *\nAllow: /\n")
    if path == "/collections/3dbars/products.json":
        page = request.url.params.get("page", "1")
        return httpx.Response(
            200, json=COLLECTION_FEED if page == "1" else {"products": []}
        )
    if path == "/collections/3dbars":
        return httpx.Response(200, html=COLLECTION_PAGE)
    if path.startswith("/products/"):
        handle = path.rsplit("/", 1)[-1]
        title = handle.replace("-", " ").title()
        return httpx.Response(
            200, html=PRODUCT_PAGE.format(title=title, handle=handle)
        )
    if path.endswith(".pdf"):
        return httpx.Response(
            200, content=_pdf_bytes(), headers={"content-type": "application/pdf"}
        )
    if path.startswith("/cdn/"):
        return httpx.Response(200, content=b"\xff\xd8\xff", headers={"content-type": "image/jpeg"})
    return httpx.Response(404)


@pytest.fixture
def fake_site(monkeypatch):
    """Point the scraper's HTTP client at the fake site."""
    transport = httpx.MockTransport(maker_handler)

    def make_client() -> httpx.Client:
        return httpx.Client(transport=transport, follow_redirects=True, timeout=5.0)

    monkeypatch.setattr(manufacturer, "client", make_client)
    monkeypatch.setattr(product_import, "source_client", make_client)
    monkeypatch.setattr(product_docs, "_default_client", make_client)
    # No sleeping through a test suite.
    monkeypatch.setattr(manufacturer.time, "sleep", lambda *_: None)
    monkeypatch.setattr(product_docs.time, "sleep", lambda *_: None)
    manufacturer_reset()
    yield
    manufacturer_reset()


def manufacturer_reset() -> None:
    from dashboard.competitors import reset_robots_cache

    reset_robots_cache()


# ── A fake Shopify ───────────────────────────────────────────────────


class FakeShopify:
    """Records what the importer sent, and answers like Shopify does."""

    def __init__(self) -> None:
        self.domain = "test.myshopify.com"
        self.products: dict[str, dict] = {}
        self.media: dict[str, list] = {}
        self.files: list[dict] = []
        self.metafields: list[dict] = []
        self.collections: list[dict] = []
        self.updates: list[dict] = []
        self._next = 100

    def _gid(self, kind: str) -> str:
        self._next += 1
        return f"gid://shopify/{kind}/{self._next}"

    def find_product(self, handle):
        return self.products.get(handle)

    def create_product(self, **kwargs):
        handle = kwargs["handle"]
        node = {
            "id": self._gid("Product"),
            "handle": handle,
            "title": kwargs["title"],
            "status": kwargs["status"],
            "descriptionHtml": kwargs["description_html"],
            "tags": kwargs.get("tags") or [],
            "seo": {
                "title": kwargs.get("seo_title"),
                "description": kwargs.get("seo_description"),
            },
            "vendor": kwargs.get("vendor"),
        }
        self.products[handle] = node
        return node

    def update_product(self, gid, **kwargs):
        self.updates.append({"id": gid, **kwargs})
        for node in self.products.values():
            if node["id"] == gid and kwargs.get("description_html"):
                node["descriptionHtml"] = kwargs["description_html"]
        return {"id": gid}

    def add_product_media(self, gid, media, dry_run=False):
        self.media.setdefault(gid, []).extend(media)
        return media

    def upload_file(self, data, filename, mime_type="application/pdf", alt=None, wait=True):
        record = {
            "id": self._gid("GenericFile"),
            "url": f"https://cdn.shopify.test/{filename}",
            "filename": filename,
            "bytes": len(data),
        }
        self.files.append(record)
        return record

    def upload_image(self, data, filename, mime_type="image/png"):
        return {"id": self._gid("MediaImage"), "url": f"https://cdn.shopify.test/{filename}"}

    def set_metafields(self, metafields):
        self.metafields.extend(metafields)
        return metafields

    def find_collection(self, handle):
        for node in self.collections:
            if node["handle"] == handle:
                return node
        return None

    def create_collection(self, **kwargs):
        node = {
            "id": self._gid("Collection"),
            "handle": kwargs.get("handle"),
            "title": kwargs["title"],
            "products": list(kwargs.get("product_gids") or []),
            "descriptionHtml": kwargs.get("description_html"),
        }
        self.collections.append(node)
        return node

    def add_products_to_collection(self, gid, product_gids):
        for node in self.collections:
            if node["id"] == gid:
                node["products"].extend(product_gids)

    def admin_url(self, gid):
        return f"https://{self.domain}/admin/products/{gid.rsplit('/', 1)[-1]}"


@pytest.fixture
def fake_shopify(monkeypatch):
    fake = FakeShopify()
    monkeypatch.setattr(product_import, "_shopify_client", lambda: fake)
    return fake


@pytest.fixture
def no_llm(monkeypatch):
    """No model call: the deterministic fallback copy is used instead.

    Everything the run does with the copy — rendering, SEO, tags, linking —
    is exercised either way, and a test that needed an API key would not run.
    """
    monkeypatch.setattr(
        product_copy, "write_copy",
        lambda source, **kwargs: (
            product_copy.tidy(
                product_copy.fallback_copy(source, vendor=kwargs.get("vendor")), source
            ),
            "test-stub",
        ),
    )


# ── Extraction ───────────────────────────────────────────────────────


def test_split_source_url_drops_the_query_a_browser_pasted():
    base, path = manufacturer.split_source_url(
        "https://www.maker.test/collections/3dbars?page=2&view=grid"
    )
    assert (base, path) == ("https://www.maker.test", "/collections/3dbars")


def test_a_shopify_collection_is_read_from_its_own_feed(fake_site):
    found = manufacturer.discover_collection("https://maker.test/collections/3dbars")
    assert found.platform == "shopify"
    assert found.title == "3D Bars"
    assert len(found.product_urls) == 2
    seed = found.seeds["https://maker.test/products/3d-bars-white"]
    assert seed.title == "3D Bars White"
    assert seed.vendor == "Ames Tile"
    assert seed.images[0].alt == "White tile"


def test_the_product_page_adds_the_documents_the_feed_never_had(fake_site):
    found = manufacturer.discover_collection("https://maker.test/collections/3dbars")
    url = "https://maker.test/products/3d-bars-white"
    product = manufacturer.fetch_product(
        url, "https://maker.test", seed=found.seeds[url]
    )
    kinds = {doc.kind for doc in product.docs}
    assert kinds == {"spec", "warranty"}
    # The spec table on the page joins whatever the feed had.
    assert product.specs["Material"] == "Ceramic"


def test_logos_and_repeat_crops_are_not_imported_as_product_photos():
    html = """
    <img src="/cdn/logo.png"><img src="/cdn/tile_600x.jpg">
    <img src="/cdn/tile.jpg"><img src="/cdn/icons/sprite.svg">
    """
    images = manufacturer.collect_images(manufacturer.soup_of(html), "https://x.test")
    assert [i.url for i in images] == ["https://x.test/cdn/tile_600x.jpg"]


def test_a_collection_with_no_product_links_says_so(fake_site, monkeypatch):
    def empty(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if request.url.path.endswith("products.json"):
            return httpx.Response(404)
        return httpx.Response(200, html="<html><body><h1>Nothing here</h1></body></html>")

    transport = httpx.MockTransport(empty)
    monkeypatch.setattr(
        manufacturer, "client",
        lambda: httpx.Client(transport=transport, follow_redirects=True),
    )
    manufacturer_reset()
    with pytest.raises(manufacturer.FetchError, match="No products found"):
        manufacturer.discover_collection("https://empty.test/collections/x")


def test_a_site_that_cannot_be_reached_says_so_rather_than_blaming_its_markup(
    monkeypatch,
):
    """An unreachable host and an empty page are different problems.

    Caught live: a proxy blocking the request produced "the page loaded, but
    nothing on it looked like a product link", which sends someone off to
    inspect markup that was never fetched.
    """
    def refused(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(refused)
    monkeypatch.setattr(
        manufacturer, "client",
        lambda: httpx.Client(transport=transport, follow_redirects=True),
    )
    manufacturer_reset()
    with pytest.raises(manufacturer.FetchError) as caught:
        manufacturer.discover_collection("https://unreachable.test/collections/x")
    message = str(caught.value)
    assert "Couldn't read" in message and "ConnectError" in message
    assert "looked like a product link" not in message
    manufacturer_reset()


def test_firecrawl_finds_the_products_a_javascript_grid_hid(fake_site, monkeypatch):
    """A collection whose grid only exists after JavaScript runs.

    The plain fetch sees an empty shell, so the render is what the import
    actually has to run on.
    """
    def shell(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if request.url.path.endswith("products.json"):
            return httpx.Response(404)
        return httpx.Response(200, html="<html><body><div id='grid'></div></body></html>")

    transport = httpx.MockTransport(shell)
    monkeypatch.setattr(
        manufacturer, "client",
        lambda: httpx.Client(transport=transport, follow_redirects=True),
    )
    monkeypatch.setattr(
        manufacturer, "_firecrawl_scrape",
        lambda url: (
            "<html><body><h1>3D Bars</h1>"
            "<a href='/products/3d-bars-white'>White</a>"
            "<a href='/products/3d-bars-grey'>Grey</a>"
            "</body></html>",
            None,
        ),
    )
    manufacturer_reset()
    found = manufacturer.discover_collection("https://js.test/collections/x")
    assert found.product_urls == [
        "https://js.test/products/3d-bars-white",
        "https://js.test/products/3d-bars-grey",
    ]
    manufacturer_reset()


def test_a_failed_render_does_not_turn_an_empty_page_into_an_unreachable_one(
    fake_site, monkeypatch
):
    """A page that loaded and had no products is still that, render or no.

    Regression: folding the render's failure into the page's own error flipped
    "nothing here looked like a product" into "couldn't reach the site, check
    your firewall" — for a page that had loaded perfectly. The render's reason
    is still worth showing, just not at the cost of the diagnosis.
    """
    def empty(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if request.url.path.endswith("products.json"):
            return httpx.Response(404)
        return httpx.Response(200, html="<html><body><h1>Nothing here</h1></body></html>")

    transport = httpx.MockTransport(empty)
    monkeypatch.setattr(
        manufacturer, "client",
        lambda: httpx.Client(transport=transport, follow_redirects=True),
    )
    monkeypatch.setattr(
        manufacturer, "_firecrawl_scrape",
        lambda url: (None, "Firecrawl answered HTTP 402"),
    )
    manufacturer_reset()
    with pytest.raises(manufacturer.FetchError) as caught:
        manufacturer.discover_collection("https://empty.test/collections/x")
    message = str(caught.value)
    assert "No products found" in message
    assert "Couldn't read" not in message
    assert "HTTP 402" in message  # the render's reason survives as a note
    manufacturer_reset()


def test_robots_disallow_stops_the_import_rather_than_being_ignored(monkeypatch):
    def blocked(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /collections/\n")
        return httpx.Response(200, html="<html></html>")

    transport = httpx.MockTransport(blocked)
    monkeypatch.setattr(
        manufacturer, "client",
        lambda: httpx.Client(transport=transport, follow_redirects=True),
    )
    manufacturer_reset()
    with pytest.raises(manufacturer.FetchError, match="robots.txt"):
        manufacturer.discover_collection("https://polite.test/collections/x")
    manufacturer_reset()


# ── Documents ────────────────────────────────────────────────────────


def test_a_pdf_is_downloaded_and_its_text_read(fake_site):
    doc = manufacturer.SourceDoc(url="https://maker.test/files/spec.pdf", kind="spec")
    product_docs.read_docs([doc])
    assert doc.error is None
    assert "20 mil" in doc.text
    assert doc.bytes_len and doc.data


def test_an_oversized_document_is_refused_before_it_is_downloaded(fake_site):
    doc = manufacturer.SourceDoc(url="https://maker.test/files/spec.pdf")
    product_docs.read_docs([doc], max_bytes=1)
    assert doc.error and "limit" in doc.error
    assert doc.text is None


def test_a_document_that_is_not_a_pdf_records_why_and_does_not_raise(fake_site):
    doc = manufacturer.SourceDoc(url="https://maker.test/cdn/x.jpg")
    product_docs.read_docs([doc])
    assert doc.error == "not a PDF, so its text wasn't read"


def test_the_filename_a_customer_downloads_is_readable():
    doc = manufacturer.SourceDoc(
        url="https://maker.test/f/9f3a2b?download=1", title="Spec Sheet", kind="spec"
    )
    assert product_docs.filename_for(doc) == "spec-sheet.pdf"


# ── Copy ─────────────────────────────────────────────────────────────


def test_an_seo_title_equal_to_the_product_title_is_changed(dashboard_db):
    source = manufacturer.SourceProduct(
        source_url="https://x.test/p", title="3D Bars White", vendor="Ames"
    )
    copy = product_copy.tidy(
        product_copy.ProductCopy(
            title="3D Bars White", product_type="Tile", summary="",
            seo_title="3D Bars White", seo_description="A tile.",
        ),
        source,
    )
    # Shopify silently stores null for an SEO title identical to the product
    # title, so a page that wanted one would quietly have none.
    assert copy.seo_title != copy.title
    assert copy.title in copy.seo_title


def test_the_no_api_key_fallback_is_tidied_like_any_other_copy(
    dashboard_db, monkeypatch
):
    """Caught by running it: the fallback returned untidied copy, so its
    seo_title equalled the product title — the value Shopify stores as null,
    leaving every product imported without a key with no meta title."""
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    from blog_pipeline.config import get_settings

    get_settings.cache_clear()
    source = manufacturer.SourceProduct(
        source_url="https://x.test/p", title="3D Bars White",
        description_text="A textured wall tile.", vendor="Ames",
    )
    copy, model = product_copy.write_copy(source)
    get_settings.cache_clear()

    assert model == "none"
    assert copy.seo_title and copy.seo_title != copy.title
    assert len(copy.seo_title) <= product_copy.MAX_SEO_TITLE


def test_tags_are_deduplicated_case_insensitively():
    assert product_copy.clean_tags(["Tile", "tile", "TILE", "Wall"]) == ["Tile", "Wall"]


def test_the_faq_becomes_structured_data_and_the_script_tag_cannot_break_out():
    copy = product_copy.ProductCopy(
        title="T", product_type="", summary="", seo_title="S", seo_description="D",
        faqs=[product_copy.FaqItem(question="Q</script><script>alert(1)", answer="A")],
    )
    rendered = product_copy.faq_jsonld(copy)
    assert '"@type": "FAQPage"' in rendered
    assert "</script><script>" not in rendered
    payload = json.loads(rendered.split(">", 1)[1].rsplit("<", 1)[0].replace("<\\/", "</"))
    assert payload["mainEntity"][0]["acceptedAnswer"]["text"] == "A"


def test_a_description_with_nothing_in_it_renders_no_empty_headings():
    copy = product_copy.ProductCopy(
        title="T", product_type="", summary="", seo_title="S", seo_description="D"
    )
    assert product_copy.render_description(copy) == ""


def test_downloads_link_our_copy_and_omit_a_document_that_failed_to_upload():
    docs = [
        manufacturer.SourceDoc(url="https://maker.test/a.pdf", title="Spec", kind="spec"),
        manufacturer.SourceDoc(url="https://maker.test/b.pdf", title="Warranty", kind="warranty"),
    ]
    rendered = product_copy.render_downloads(
        docs, {"https://maker.test/a.pdf": "https://cdn.shopify.test/a.pdf"}
    )
    assert "cdn.shopify.test/a.pdf" in rendered
    assert "maker.test/b.pdf" not in rendered


# ── A whole run ──────────────────────────────────────────────────────


def _drive(run_id: int, passes: int = 20) -> None:
    for _ in range(passes):
        result = product_import.advance(run_id)
        if result.done:
            return
    raise AssertionError("the run never finished")


def test_a_collection_becomes_draft_products_a_collection_and_cross_links(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    run_id = product_import.start_run("https://maker.test/collections/3dbars")
    _drive(run_id)

    status = product_import.run_status(run_id)
    assert status["stage"] == ImportStage.done.value
    assert status["counts"]["created"] == 2

    # Products: drafts, with the vendor and SEO fields from the source.
    assert set(fake_shopify.products) == {"3d-bars-white", "3d-bars-black"}
    white = fake_shopify.products["3d-bars-white"]
    assert white["status"] == "DRAFT"
    assert white["vendor"] == "Ames Tile"
    assert white["seo"]["title"] and white["seo"]["title"] != white["title"]
    assert "imported" in white["tags"]

    # Pictures: handed to Shopify as URLs for it to fetch and store, with alt
    # text. The manufacturer's own feed is the gallery when there is one — the
    # product page's <img> tags are not merged in on top, because a theme's
    # markup is full of banners and related-product thumbnails.
    media = fake_shopify.media[white["id"]]
    assert [item["originalSource"] for item in media] == [
        "https://cdn.maker.test/white.jpg"
    ]
    assert all(item["alt"] for item in media)

    # Documents: downloaded, read, and re-hosted on our store.
    assert len(fake_shopify.files) == 4  # spec + warranty, per product
    assert "cdn.shopify.test" in white["descriptionHtml"]
    assert "Specifications" in white["descriptionHtml"]

    # The specs came out of the page's table, into a metafield as data.
    specs = [m for m in fake_shopify.metafields if m["key"] == "specifications"]
    assert specs and "Ceramic" in specs[0]["value"]

    # The collection holds both products.
    assert len(fake_shopify.collections) == 1
    assert len(fake_shopify.collections[0]["products"]) == 2

    # And each product links to the other, with a picture. The heading names
    # the range rather than saying "this collection": the range's own name is
    # the phrase a reader would search for, and it gives the internal links
    # an anchor context that says what they are.
    assert "you can find them in the following list" in white["descriptionHtml"]
    assert "3D Bars" in white["descriptionHtml"]
    assert "3D Bars Black" in white["descriptionHtml"]

    # The tags the storefront is built on are present, not merely likely: a
    # smart collection defined as brand + collection needs both on every
    # product in the range, every time.
    assert "3D Bars" in white["tags"]        # the collection
    assert "Ames Tile" in white["tags"]      # the brand
    related = [m for m in fake_shopify.metafields if m["key"] == "related_products"]
    assert len(related) == 2


def test_a_dry_run_reads_everything_and_creates_nothing(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    run_id = product_import.start_run(
        "https://maker.test/collections/3dbars", dry_run=True
    )
    _drive(run_id)

    status = product_import.run_status(run_id)
    assert status["stage"] == ImportStage.done.value
    assert status["counts"]["prepared"] == 2
    assert not fake_shopify.products and not fake_shopify.collections

    # The extraction is still all there to look at — that's the point of it.
    detail = product_import.run_detail(run_id)
    first = detail["products"][0]
    assert first["extracted"]["specs"]["Finish"] == "Matte"
    assert first["generated"]["seo_description"]


def test_re_running_an_import_leaves_the_products_it_already_made_alone(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    _drive(product_import.start_run("https://maker.test/collections/3dbars"))
    created_ids = {node["id"] for node in fake_shopify.products.values()}

    _drive(product_import.start_run("https://maker.test/collections/3dbars"))

    assert {node["id"] for node in fake_shopify.products.values()} == created_ids
    with get_session() as session:
        second = session.query(ImportRun).order_by(ImportRun.id.desc()).first()
        statuses = {
            row.status
            for row in session.query(ImportProduct)
            .filter(ImportProduct.run_id == second.id)
            .all()
        }
    assert statuses == {ImportProductStatus.skipped.value}


def test_a_product_whose_page_dies_fails_alone(
    dashboard_db, fake_site, fake_shopify, no_llm, monkeypatch
):
    real_fetch = manufacturer.fetch_product

    def one_bad(url, base, **kwargs):
        if "black" in url:
            raise httpx.ConnectError("the supplier's server hung up")
        return real_fetch(url, base, **kwargs)

    monkeypatch.setattr(product_import, "fetch_product", one_bad)
    run_id = product_import.start_run("https://maker.test/collections/3dbars")
    _drive(run_id)

    status = product_import.run_status(run_id)
    assert status["counts"]["created"] == 1
    assert status["counts"]["failed"] == 1
    assert status["stage"] == ImportStage.done.value
    with get_session() as session:
        failed = (
            session.query(ImportProduct)
            .filter(ImportProduct.status == ImportProductStatus.failed.value)
            .one()
        )
    assert "hung up" in failed.error


def test_a_run_resumes_where_the_last_pass_stopped(
    dashboard_db, fake_site, fake_shopify, no_llm, monkeypatch
):
    # One product per pass, which is what a 60-second function looks like
    # against a slow supplier.
    monkeypatch.setattr(product_import, "PASS_BUDGET_SECONDS", 0.0)
    monkeypatch.setattr(product_import, "LOCAL_PASS_BUDGET_SECONDS", 0.0)

    run_id = product_import.start_run("https://maker.test/collections/3dbars")
    product_import.advance(run_id)  # discover
    product_import.advance(run_id)  # one product, then out of budget

    assert len(fake_shopify.products) == 1
    _drive(run_id)
    assert len(fake_shopify.products) == 2


def test_a_collection_that_cannot_be_read_fails_the_run_with_the_reason(
    dashboard_db, fake_site, fake_shopify
):
    run_id = product_import.start_run("https://maker.test/collections/missing")
    result = product_import.advance(run_id)
    assert result.stage == ImportStage.failed.value
    status = product_import.run_status(run_id)
    assert not status["active"]
    # The URL 404s, and the run says exactly that rather than reporting on
    # markup it never received.
    assert "HTTP 404" in status["error"]


def test_stopping_a_run_keeps_what_it_already_created(
    dashboard_db, fake_site, fake_shopify, no_llm, monkeypatch
):
    monkeypatch.setattr(product_import, "PASS_BUDGET_SECONDS", 0.0)
    monkeypatch.setattr(product_import, "LOCAL_PASS_BUDGET_SECONDS", 0.0)
    run_id = product_import.start_run("https://maker.test/collections/3dbars")
    product_import.advance(run_id)
    product_import.advance(run_id)
    product_import.stop_run(run_id)

    assert len(fake_shopify.products) == 1
    status = product_import.run_status(run_id)
    assert status["stage"] == ImportStage.stopped.value
    assert not status["active"]
    # A stopped run is not picked up again by the cron job.
    assert run_id not in product_import.active_run_ids()


# ── The pages ────────────────────────────────────────────────────────


@pytest.fixture
def client(dashboard_db):
    from fastapi.testclient import TestClient

    from dashboard.web import create_app

    with TestClient(create_app()) as c:
        yield c


def test_the_import_page_renders_with_no_runs(client):
    response = client.get("/import")
    assert response.status_code == 200
    assert "Manufacturer collection URL" in response.text


def test_submitting_the_form_creates_a_run_and_redirects_to_it(client, fake_site):
    response = client.post(
        "/import",
        data={
            "source_url": "https://maker.test/collections/3dbars",
            "vendor": "Ames Tile",
            "dry_run": "1",
            "make_collection": "1",
            "link_products": "1",
            "max_products": "10",
            "publish_status": "DRAFT",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/import/")

    with get_session() as session:
        run = session.query(ImportRun).one()
    assert run.dry_run is True
    assert run.options["max_products"] == 10
    # Nothing was fetched by the form post itself — the run page does the work.
    assert run.stage == ImportStage.discover.value

    assert client.get(location).status_code == 200


def test_the_run_page_advances_one_pass_per_request(
    client, fake_site, fake_shopify, no_llm
):
    run_id = product_import.start_run("https://maker.test/collections/3dbars")
    body = client.post(f"/import/{run_id}/advance").json()
    assert body["advanced"] is True
    assert body["stage"] == ImportStage.products.value
    assert body["counts"]["total"] == 2

    status = client.get(f"/import/{run_id}/status").json()
    assert status["active"] is True


def test_the_run_page_shows_what_a_finished_run_created(
    client, fake_site, fake_shopify, no_llm
):
    run_id = product_import.start_run("https://maker.test/collections/3dbars")
    _drive(run_id)

    page = client.get(f"/import/{run_id}").text
    assert "3D Bars White" in page
    assert "Shopify admin" in page
    # And the run list links to it.
    assert f"/import/{run_id}" in client.get("/import").text


def test_an_unknown_run_is_a_404_not_a_crash(client):
    assert client.get("/import/999/status").status_code == 404


def test_a_run_without_a_brand_is_refused_before_any_work(client, fake_site):
    """Every product name begins with the brand, and most suppliers publish
    it nowhere a scraper can read. Starting the run anyway would spend a
    scrape and a model call per product on a range named wrongly."""
    response = client.post(
        "/import",
        data={"source_url": "https://maker.test/collections/3dbars",
              "vendor": "  ", "dry_run": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert "/import/" not in response.headers["location"]
