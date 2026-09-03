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


#: A range in three sizes, which is the ordinary case and was the broken
#: one: Ames' "Advantage" is four colours in 24"x24", 24"x48" and 36"x72".
#: The maker writes the size into every title, exactly like this.
_ADVANTAGE_SIZES = ('24"x 24"', '24"x 48"', '36"x 72"')
#: Colour, and the maker's own two-letter code for it — "Greige" and
#: "Graphite" both start "Gr", and the source handles have to stay distinct
#: or the test would be about the wrong collision.
_ADVANTAGE_COLOURS = (
    ("Chalk", "ch"), ("Graphite", "ga"), ("Greige", "ge"), ("Silver", "sv"),
)


def _advantage_feed() -> dict:
    products = []
    for size in _ADVANTAGE_SIZES:
        for colour, code in _ADVANTAGE_COLOURS:
            index = len(products) + 1
            digits = "".join(c for c in size if c.isdigit())
            handle = "ad" + code + "m" + digits
            products.append({
                "id": index,
                "handle": handle,
                "title": "Advantage | " + size + " " + colour + " Matte",
                "body_html": "<p>Through-body porcelain.</p>",
                "vendor": "Ames Tile",
                "product_type": "Porcelain Tile",
                "images": [{"src": "https://cdn.maker.test/ad%d.jpg" % index}],
                "variants": [{"sku": "AD-%d" % index}],
            })
    return {"products": products}


ADVANTAGE_FEED = _advantage_feed()


def maker_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/robots.txt":
        return httpx.Response(200, text="User-agent: *\nAllow: /\n")
    if path == "/collections/advantage/products.json":
        page = request.url.params.get("page", "1")
        return httpx.Response(
            200, json=ADVANTAGE_FEED if page == "1" else {"products": []}
        )
    if path == "/collections/advantage":
        return httpx.Response(
            200,
            html='<html><head><meta name="description" content="Advantage.">'
                 "</head><body><h1>Advantage</h1></body></html>",
        )
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
        self.published: list[str] = []
        #: {key: type} the store has defined, and every value written under
        #: a key that had no definition at the time.
        self.definitions: dict[str, str] = {}
        self.undefined_writes: list[dict] = []
        self._next = 100

    def _gid(self, kind: str) -> str:
        self._next += 1
        return f"gid://shopify/{kind}/{self._next}"

    def find_product(self, handle):
        node = self.products.get(handle)
        if node is None:
            return None
        # Shopify reports this on the lookup, and the importer's overwrite
        # reads it to decide whether to add its pictures or leave the ones
        # the product already has.
        return {**node, "mediaCount": {"count": len(self.media.get(node["id"], []))}}

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

    #: What `update_product` writes onto the stored node, and under what
    #: name. Applied for real rather than only recorded, because the
    #: importer's overwrite is only correct if the product afterwards is the
    #: new product — a fake that records the call and keeps the old title
    #: would pass a test that changed nothing.
    _UPDATABLE = {
        "title": "title",
        "description_html": "descriptionHtml",
        "handle": "handle",
        "vendor": "vendor",
        "product_type": "productType",
        "tags": "tags",
        "status": "status",
    }

    def update_product(self, gid, **kwargs):
        self.updates.append({"id": gid, **kwargs})
        for handle, node in list(self.products.items()):
            if node["id"] != gid:
                continue
            for arg, field in self._UPDATABLE.items():
                if kwargs.get(arg) is not None:
                    node[field] = kwargs[arg]
            if kwargs.get("seo_title") or kwargs.get("seo_description"):
                node["seo"] = {
                    "title": kwargs.get("seo_title"),
                    "description": kwargs.get("seo_description"),
                }
            if node["handle"] != handle:
                del self.products[handle]
                self.products[node["handle"]] = node
            return node
        return {"id": gid}

    def add_product_media(self, gid, media, dry_run=False):
        self.media.setdefault(gid, []).extend(media)
        # Ids, because the caller now has to ask what became of each one:
        # the real mutation only queues the fetch.
        return [
            {"id": self._gid("MediaImage"), "status": "PROCESSING"}
            for _ in media
        ]

    def wait_for_media(self, media_ids, **kwargs):
        """This fake's Shopify always manages the fetch."""
        return {mid: "READY" for mid in media_ids if mid}

    def list_publications(self):
        return [{"id": "gid://shopify/Publication/1", "name": "Online Store"}]

    def publish_to_all_channels(self, resource_gid):
        self.published.append(resource_gid)
        return ["Online Store"]

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

    #: Definitions the store has, keyed by metafield key. The importer asks
    #: for these before it writes: a value written under a key the store has
    #: not defined is stored by Shopify and shown by nothing, which is what
    #: an empty Metafields panel on a fully imported product means.
    def ensure_metafield_definitions(self, definitions, *, namespace,
                                     owner_type="PRODUCT"):
        created = []
        conflicting = []
        for definition in definitions:
            key, wanted = definition["key"], definition["type"]
            existing = self.definitions.get(key)
            if existing is None:
                self.definitions[key] = wanted
                created.append(key)
            elif existing != wanted:
                conflicting.append(f"{key} (defined as {existing})")
        return created, conflicting

    def set_metafields(self, metafields):
        # Shopify stores a value whether or not the key is defined. What a
        # definition changes is whether anyone can see it — so the fake
        # records both, and a test can ask either question.
        for field in metafields:
            self.undefined_writes.extend(
                [field] if field["key"] not in self.definitions else []
            )
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


def test_a_collection_becomes_live_products_a_collection_and_cross_links(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    run_id = product_import.start_run("https://maker.test/collections/3dbars")
    _drive(run_id)

    status = product_import.run_status(run_id)
    assert status["stage"] == ImportStage.done.value
    assert status["counts"]["created"] == 2

    # Products: drafts, with the vendor and SEO fields from the source.
    # Handles follow the composed title, not the supplier's own: brand,
    # range, type, then the variant read off their title ("White").
    assert set(fake_shopify.products) == {
        "ames-tile-3d-bars-wall-tile-white",
        "ames-tile-3d-bars-wall-tile-black",
    }
    white = fake_shopify.products["ames-tile-3d-bars-wall-tile-white"]
    # Active and on every channel, because an import that finished Draft
    # needed a second manual pass in Shopify admin to be worth anything, and
    # that pass was easy to forget. Both are settings; these are the defaults.
    assert white["status"] == "ACTIVE"
    assert white["id"] in fake_shopify.published
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
    # The sibling is linked under its composed name, the one it carries in
    # the store — not the supplier's title it was imported from.
    assert "Ames Tile 3D Bars Wall Tile - Black" in white["descriptionHtml"]

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


def test_a_range_in_three_sizes_imports_as_twelve_products(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    """The size belongs to the product, not to the range.

    Ames' "Advantage" is four colours in 24"x24", 24"x48" and 36"x72". The
    range's shape was settled from the first product and applied to the rest,
    so all twelve composed the name of the first four — and since the handle
    follows the name, the other eight found a product sitting on their handle
    and were logged as "already in the store, left untouched". Twelve products
    in, four products out, and the run reported success.
    """
    run_id = product_import.start_run(
        "https://maker.test/collections/advantage", vendor="Ames Tile & Stone"
    )
    _drive(run_id)

    status = product_import.run_status(run_id)
    assert status["counts"]["created"] == 12
    assert status["counts"]["skipped"] == 0
    assert len(fake_shopify.products) == 12

    # Every size survives into the names, and every one of the twelve is
    # distinct — which is the whole of what went wrong.
    titles = {node["title"] for node in fake_shopify.products.values()}
    assert len(titles) == 12
    for size in ('24"x24"', '24"x48"', '36"x72"'):
        assert sum(1 for t in titles if size in t) == 4


def test_two_products_that_compose_one_name_are_not_reported_as_yours(
    dashboard_db, fake_site, fake_shopify, no_llm, monkeypatch
):
    """The safety net under the naming, for when the naming is wrong anyway.

    "Already in the store" and "we just gave two of the supplier's products
    the same name" look identical from Shopify — both are a product sitting
    on the handle. They are not identical from the run: a row of this same
    run claimed that handle, from a different source page, so these are two
    different products by construction. Nothing has to be guessed, so nothing
    is skipped.
    """
    # Naming that cannot tell the sizes apart, which is what the old bug
    # amounted to.
    monkeypatch.setattr(
        product_import.product_copy, "derive_size", lambda *a, **k: '24"x24"'
    )

    run_id = product_import.start_run(
        "https://maker.test/collections/advantage", vendor="Ames Tile & Stone"
    )
    _drive(run_id)

    status = product_import.run_status(run_id)
    # Still twelve products in the store, and not one of them written off as
    # already being there.
    assert status["counts"]["created"] == 12
    assert status["counts"]["skipped"] == 0
    assert len(fake_shopify.products) == 12
    # The eight that collided went in beside the four that did not, and the
    # log says so rather than claiming they were already yours.
    assert sum(1 for h in fake_shopify.products if h.endswith(("-2", "-3"))) == 8
    with get_session() as session:
        run = session.get(ImportRun, run_id)
        log = "\n".join(run.log)
    assert "named the same as" in log


# ── Metafields, and being able to see them ───────────────────────────
#
# Writing a metafield does not make it visible. Shopify stores the value
# either way, but the admin's Metafields section lists only keys the store
# has a *definition* for, and the theme editor offers only those as dynamic
# sources. An import that writes a full specifications table into a product
# whose Metafields panel is empty has, from the only side anyone can see,
# written nothing.


def test_an_import_defines_the_metafields_it_writes(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    run_id = product_import.start_run("https://maker.test/collections/3dbars")
    _drive(run_id)

    # Every key this import can write is defined, with the type it writes.
    for definition in product_import.METAFIELD_DEFINITIONS:
        assert fake_shopify.definitions[definition["key"]] == definition["type"]

    # And nothing was written under a key that had no definition yet.
    assert fake_shopify.undefined_writes == []

    with get_session() as session:
        run = session.get(ImportRun, run_id)
        log = "\n".join(run.log)
    assert "Defined 5 metafields" in log


def test_the_range_metafield_is_defined_even_when_nothing_is_created(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    """`related_products` is written by the linking stage and by no other.

    A re-import whose products all already exist never reaches a create, so
    the definitions have to be settled by the stage that needs them rather
    than as a side effect of making something.
    """
    _drive(product_import.start_run("https://maker.test/collections/3dbars"))
    fake_shopify.definitions.clear()
    fake_shopify.undefined_writes.clear()

    _drive(product_import.start_run("https://maker.test/collections/3dbars"))

    assert "related_products" in fake_shopify.definitions
    assert fake_shopify.undefined_writes == []


def test_a_store_that_already_defined_a_key_differently_is_told_not_corrected(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    """Changing the type of a definition that already has values under it is
    destructive, and belongs to whoever made it. Saying so is not."""
    fake_shopify.definitions["specifications"] = "multi_line_text_field"

    run_id = product_import.start_run("https://maker.test/collections/3dbars")
    _drive(run_id)

    # Left exactly as the store had it.
    assert fake_shopify.definitions["specifications"] == "multi_line_text_field"
    with get_session() as session:
        run = session.get(ImportRun, run_id)
        log = "\n".join(run.log)
    assert "already defined with a different type" in log
    assert "specifications (defined as multi_line_text_field)" in log


def test_metafields_that_could_not_be_defined_do_not_fail_the_import(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    """A token without `write_metafield_definitions` still imports products.

    It gets them with invisible metafields, and is told once — rather than
    left to notice an empty panel and wonder which of the two happened.
    """
    from blog_pipeline.tools.shopify import ShopifyError

    def refused(definitions, *, namespace, owner_type="PRODUCT"):
        raise ShopifyError("Access denied for metafieldDefinitionCreate")

    fake_shopify.ensure_metafield_definitions = refused

    run_id = product_import.start_run("https://maker.test/collections/3dbars")
    _drive(run_id)

    status = product_import.run_status(run_id)
    assert status["counts"]["created"] == 2
    assert status["stage"] == ImportStage.done.value
    with get_session() as session:
        run = session.get(ImportRun, run_id)
        log = "\n".join(run.log)
    assert "Could not define this store's metafields" in log
    # The values still went, which is the half that survives.
    assert any(m["key"] == "specifications" or m["key"] == "source_url"
               for m in fake_shopify.metafields)


def test_a_metafield_that_shopify_refuses_is_said_out_loud(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    """It used to be a `log.info` into a serverless function's stderr, which
    is the same as silence: the run reported the product created, counted its
    images and documents, and said nothing about the structured data it had
    just failed to write."""
    from blog_pipeline.tools.shopify import ShopifyError

    def refuse(metafields):
        raise ShopifyError("metafieldsSet userErrors: [{'message': 'Value is invalid'}]")

    fake_shopify.set_metafields = refuse

    run_id = product_import.start_run("https://maker.test/collections/3dbars")
    _drive(run_id)

    # Not fatal — the product is the product, with or without its metafields.
    status = product_import.run_status(run_id)
    assert status["counts"]["created"] == 2
    assert status["counts"]["failed"] == 0
    with get_session() as session:
        run = session.get(ImportRun, run_id)
        log = "\n".join(run.log)
    assert "were not written" in log
    assert "Value is invalid" in log


# ── Overruling a skip ────────────────────────────────────────────────
#
# The skip is decided on the handle, so it answers "is there a product called
# this" rather than "have we imported this product". When those two come
# apart the owner is missing a product they asked for, and these are the two
# ways out of it. Both go through the ordinary stages afterwards — that is
# most of the point, and what these tests are really pinning down.


def _second_run_of_the_same_collection() -> int:
    """Import the range twice. The second run skips everything."""
    _drive(product_import.start_run("https://maker.test/collections/3dbars"))
    run_id = product_import.start_run("https://maker.test/collections/3dbars")
    _drive(run_id)
    return run_id


def test_a_skipped_product_can_be_rewritten_in_place_on_request(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    run_id = _second_run_of_the_same_collection()
    before = {node["id"] for node in fake_shopify.products.values()}
    assert product_import.run_status(run_id)["counts"]["skipped"] == 2

    queued = product_import.reimport_skipped(run_id, mode=product_import.FORCE_UPDATE)
    assert queued == 2
    # Reopened rather than replaced: the range is one range, and a fresh run
    # would cross-link these two to each other instead of to their siblings.
    assert product_import.run_status(run_id)["active"]
    _drive(run_id)

    status = product_import.run_status(run_id)
    assert status["stage"] == ImportStage.done.value
    assert status["counts"]["updated"] == 2
    assert status["counts"]["skipped"] == 0
    # Nothing new in the store, and nothing removed from it.
    assert {node["id"] for node in fake_shopify.products.values()} == before

    # The whole product, not half of it: the overwrite writes every field the
    # create would have written.
    written = [u for u in fake_shopify.updates if u.get("title")]
    assert len(written) == 2
    assert all(u["tags"] and u["seo_title"] and u["description_html"] for u in written)
    assert all(u["vendor"] == "Ames Tile" for u in written)

    with get_session() as session:
        rows = (
            session.query(ImportProduct)
            .filter(ImportProduct.run_id == run_id)
            .all()
        )
    assert {r.force_mode for r in rows} == {product_import.FORCE_UPDATE}
    assert all(r.admin_url for r in rows)


def test_an_overwrite_keeps_the_pictures_the_product_already_has(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    """Shopify's media is additive — there is no "replace the images" — so a
    rewrite that re-attached them would leave the product showing both sets."""
    run_id = _second_run_of_the_same_collection()
    before = {gid: list(media) for gid, media in fake_shopify.media.items()}
    assert before  # the first run did attach pictures

    product_import.reimport_skipped(run_id, mode=product_import.FORCE_UPDATE)
    _drive(run_id)

    assert {gid: list(m) for gid, m in fake_shopify.media.items()} == before
    with get_session() as session:
        rows = (
            session.query(ImportProduct)
            .filter(ImportProduct.run_id == run_id)
            .all()
        )
    assert all(r.images_saved == 0 for r in rows)


def test_a_skipped_product_can_be_imported_beside_the_one_in_the_store(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    """The other mistake: the handle collides but the products are different.

    Nothing is overwritten — the product goes in under the next free handle,
    which is the only thing Shopify will accept and the only thing the owner
    asked for.
    """
    run_id = _second_run_of_the_same_collection()
    before = {handle: node["id"] for handle, node in fake_shopify.products.items()}

    product_import.reimport_skipped(run_id, mode=product_import.FORCE_NEW)
    _drive(run_id)

    status = product_import.run_status(run_id)
    assert status["counts"]["created"] == 2
    assert status["counts"]["updated"] == 0
    # The originals are untouched and still on their own handles.
    for handle, gid in before.items():
        assert fake_shopify.products[handle]["id"] == gid
    fresh = {h for h in fake_shopify.products if h not in before}
    assert fresh == {h + "-2" for h in before}


def test_one_row_can_be_overruled_without_the_others(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    run_id = _second_run_of_the_same_collection()
    with get_session() as session:
        target = (
            session.query(ImportProduct)
            .filter(ImportProduct.run_id == run_id)
            .order_by(ImportProduct.position)
            .first()
        )
        target_id = target.id

    assert product_import.reimport_skipped(
        run_id, mode=product_import.FORCE_UPDATE, product_ids=[target_id]
    ) == 1
    _drive(run_id)

    status = product_import.run_status(run_id)
    assert status["counts"]["updated"] == 1
    assert status["counts"]["skipped"] == 1


def test_overruling_one_product_re_links_the_siblings_that_were_not(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    """A product rewritten after the grids were written is invisible to them.

    The linking stage only touches what is marked unlinked, so overruling a
    skip has to unlink the siblings too. Otherwise the one product whose copy
    just changed keeps the title, the picture and the URL its siblings
    recorded before it changed — which is the version the owner overruled.
    """
    run_id = _second_run_of_the_same_collection()
    with get_session() as session:
        rows = (
            session.query(ImportProduct)
            .filter(ImportProduct.run_id == run_id)
            .order_by(ImportProduct.position)
            .all()
        )
        first_id, second_gid = rows[0].id, rows[1].product_gid

    fake_shopify.updates.clear()
    product_import.reimport_skipped(
        run_id, mode=product_import.FORCE_UPDATE, product_ids=[first_id]
    )
    _drive(run_id)

    with get_session() as session:
        rows = (
            session.query(ImportProduct)
            .filter(ImportProduct.run_id == run_id)
            .all()
        )
    assert all(r.linked for r in rows)
    # The sibling nobody asked about was rewritten too, because what it says
    # about the other product is now out of date.
    assert any(u["id"] == second_gid for u in fake_shopify.updates)
    # And every product in the range still carries the other one.
    by_gid = {node["id"]: node for node in fake_shopify.products.values()}
    for row in rows:
        body = by_gid[row.product_gid]["descriptionHtml"]
        others = [r for r in rows if r.id != row.id]
        assert all(f"/products/{o.handle}" in body for o in others)


def test_a_dry_run_has_no_skips_to_overrule(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    """Nothing was sent to Shopify, so nothing was skipped for being there."""
    run_id = product_import.start_run(
        "https://maker.test/collections/3dbars", dry_run=True
    )
    _drive(run_id)
    with pytest.raises(product_import.ImportRunError):
        product_import.reimport_skipped(run_id)


def test_an_unknown_override_is_refused_rather_than_guessed(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    run_id = _second_run_of_the_same_collection()
    with pytest.raises(product_import.ImportRunError):
        product_import.reimport_skipped(run_id, mode="overwrite-everything")
    # And the run is left exactly as it was.
    assert not product_import.run_status(run_id)["active"]


# ── The controls on the run page ─────────────────────────────────────


@pytest.fixture
def web(dashboard_db):
    from fastapi.testclient import TestClient

    from dashboard.web import create_app

    with TestClient(create_app()) as c:
        yield c


def test_the_run_page_offers_a_way_out_of_every_skip(
    web, fake_site, fake_shopify, no_llm
):
    run_id = _second_run_of_the_same_collection()
    page = web.get(f"/import/{run_id}").text
    assert f"/import/{run_id}/reimport" in page
    # Both mistakes, both offered — the app cannot tell them apart.
    assert 'value="update"' in page
    assert 'value="new"' in page


def test_posting_the_override_queues_the_products_and_reopens_the_run(
    web, fake_site, fake_shopify, no_llm
):
    run_id = _second_run_of_the_same_collection()
    assert not product_import.run_status(run_id)["active"]

    response = web.post(
        f"/import/{run_id}/reimport", data={"mode": "update"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/import/{run_id}"

    status = product_import.run_status(run_id)
    assert status["active"]
    assert status["stage"] == ImportStage.products.value
    assert status["counts"]["pending"] == 2

    # And the page still renders once they have been, with the record of
    # what was done to them on the row.
    _drive(run_id)
    page = web.get(f"/import/{run_id}").text
    assert "rewritten on request" in page
    assert "Rewritten" in page


def test_an_override_with_nothing_to_do_says_so_instead_of_pretending(
    web, fake_site, fake_shopify, no_llm
):
    """The first run skipped nothing, so there is nothing to overrule — and
    a redirect that looked like success would leave the owner watching a run
    page for work that was never queued."""
    run_id = product_import.start_run("https://maker.test/collections/3dbars")
    _drive(run_id)

    response = web.post(
        f"/import/{run_id}/reimport", data={"mode": "update"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert not product_import.run_status(run_id)["active"]


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


# ── Sales channels ─────────────────────────────────────────────────
#
# Status and channels are two switches, and a product needs both: Active
# decides whether it is for sale, publications decide which channels carry
# it. An import that never mentioned channels reached whichever ones had
# "automatically publish new products" left on — which looks exactly like
# working until a store has one of them off.


class ChannelShopify(FakeShopify):
    """A store with five sales channels, recording what gets published."""

    def __init__(self, fail=False):
        super().__init__()
        self.published: list[str] = []
        self.fail = fail

    def list_publications(self):
        return [
            {"id": f"gid://shopify/Publication/{n}", "name": name}
            for n, name in enumerate(
                ["Online Store", "Buy Button", "Google & YouTube", "Shop",
                 "Facebook & Instagram"], start=1,
            )
        ]

    def publish_to_all_channels(self, resource_gid):
        from blog_pipeline.tools.shopify import ShopifyError

        if self.fail:
            raise ShopifyError("publishablePublish: access denied")
        self.published.append(resource_gid)
        return [p["name"] for p in self.list_publications()]


def test_a_new_product_is_published_to_every_channel(
    dashboard_db, fake_site, no_llm, monkeypatch
):
    fake = ChannelShopify()
    monkeypatch.setattr(product_import, "_shopify_client", lambda: fake)
    run_id = product_import.start_run(
        "https://maker.test/collections/3dbars", vendor="Ames Tile",
    )
    for _ in range(40):
        if product_import.advance(run_id).done:
            break

    assert len(fake.published) == len(fake.products)
    assert fake.published


def test_a_channel_refusal_does_not_fail_the_import(
    dashboard_db, fake_site, no_llm, monkeypatch
):
    """The product exists and is correct. Losing the run over a permission
    problem would throw away the work that did succeed."""
    fake = ChannelShopify(fail=True)
    monkeypatch.setattr(product_import, "_shopify_client", lambda: fake)
    run_id = product_import.start_run(
        "https://maker.test/collections/3dbars", vendor="Ames Tile",
    )
    for _ in range(40):
        if product_import.advance(run_id).done:
            break

    assert fake.products
    with get_session() as session:
        assert session.get(ImportRun, run_id).error is None


def test_publishing_can_be_turned_off(
    dashboard_db, fake_site, no_llm, monkeypatch
):
    from dashboard import store

    store.set(store.IMPORT_ALL_CHANNELS, False)
    fake = ChannelShopify()
    monkeypatch.setattr(product_import, "_shopify_client", lambda: fake)
    run_id = product_import.start_run(
        "https://maker.test/collections/3dbars", vendor="Ames Tile",
    )
    for _ in range(40):
        if product_import.advance(run_id).done:
            break

    assert fake.products
    assert fake.published == []
