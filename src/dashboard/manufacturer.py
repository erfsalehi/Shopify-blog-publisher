"""Reading a manufacturer's collection page, and the products in it.

This is the input end of the product importer: the owner pastes a URL like
`https://www.example.com/collections/3dbars` and this module turns it into
product records — title, description, images, spec documents, specifications.

The order of preference mirrors `competitors.py`, and for the same reason:
take the machine-readable feed when there is one, fall back to the markup the
site publishes for Google, and only then guess at HTML.

  1. **Shopify's collection JSON** (`/collections/<handle>/products.json`).
     Most manufacturers and distributors run Shopify, and this hands over the
     whole product — title, `body_html`, every image with its alt text, tags,
     vendor, options — in one request per 250 products. When it answers,
     nothing below this line has to be guessed at.
  2. **JSON-LD** (`schema.org/Product`, `ItemList`). Sites emit it for
     Google's benefit; it carries name, description, brand, sku, images and
     often a `additionalProperty` spec list.
  3. **OpenGraph** — `og:title`, `og:description`, `og:image` — which
     storefronts emit for Facebook when they emit nothing else.
  4. **The HTML itself**: the `<h1>`, the spec `<table>`, the `<img>` tags in
     the main content, and the `<a href="...pdf">` links that are the whole
     point of the exercise.

Every step contributes what it has and leaves the rest alone, so a page with
good JSON-LD and a PDF link buried in the markup gets both. What produced
each field is recorded in `sources` on the result, because a description that
came from an `og:description` (155 characters, truncated) is a different
thing from one that came from `body_html`, and the copy stage should know.

Politeness is inherited wholesale from `competitors.py` — the same robots.txt
check, the same pause between requests, a User-Agent that says who this is.
A supplier's site is not a resource to strip-mine, and this fetches a
collection at roughly the pace of a person reading it.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import parse_qs, urljoin, urlparse, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from dashboard.competitors import FetchError, may_fetch

log = logging.getLogger(__name__)

#: Seconds between requests to the same host.
PAUSE = 1.0
TIMEOUT = 25.0

#: Says what this is and who it's for. Deliberately not the competitor
#: monitor's User-Agent: this reads a supplier's own catalogue, usually with
#: their blessing, and a site owner reading their logs should be able to tell
#: the two apart. The `DRFlooringControlCenter` prefix is shared so a
#: robots.txt rule written for one applies to both — see
#: `competitors.ROBOTS_AGENT`.
USER_AGENT = (
    "DRFlooringControlCenter/1.0 (+https://drflooring.ca; "
    "supplier catalogue import; contact via drflooring.ca)"
)

#: Pages of a paginated collection to walk in one discovery pass.
MAX_COLLECTION_PAGES = 10

FIRECRAWL_BASE_URL = "https://api.firecrawl.dev/v2"

#: File extensions treated as product documentation.
DOC_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx")

#: Link text that means "this is a document worth having" even when the href
#: doesn't end in .pdf (a redirect, a CDN download endpoint, a query string).
DOC_TEXT_MARKERS = (
    "spec", "tech", "data sheet", "datasheet", "tds", "sds", "msds",
    "warranty", "install", "guide", "manual", "brochure", "catalog",
    "catalogue", "declaration", "certificate", "maintenance", "care",
    "download", "cut sheet", "submittal",
)

#: Image URLs that are furniture, not product photography.
_IMAGE_NOISE = re.compile(
    r"(logo|icon|sprite|placeholder|swatch-?nav|payment|badge|avatar|favicon"
    r"|loading|spinner|blank|pixel|1x1|thumb-?nav|flag)",
    re.I,
)

_LD_TYPES_PRODUCT = {"product", "productgroup", "individualproduct"}


@dataclass
class SourceImage:
    url: str
    alt: str | None = None
    position: int = 0


@dataclass
class SourceDoc:
    """A document linked from the product page.

    `text` and `data` are filled in later by `product_docs.py` — this module
    only ever finds them. Kept as one object through the whole pipeline so
    the thing that gets uploaded to Shopify is provably the same thing whose
    text went into the description.
    """

    url: str
    title: str | None = None
    kind: str = "other"
    text: str | None = None
    pages: int | None = None
    bytes_len: int | None = None
    error: str | None = None
    data: bytes | None = None

    def as_dict(self) -> dict:
        """Everything but the bytes — this is what gets stored on the row."""
        return {
            "url": self.url, "title": self.title, "kind": self.kind,
            "pages": self.pages, "bytes_len": self.bytes_len,
            "error": self.error,
            # Truncated: the full text of a 60-page installation manual is
            # not something to keep a copy of per product, and the copy
            # stage never sees more than this anyway.
            "text": (self.text or "")[:20000] or None,
        }


@dataclass
class SourceProduct:
    source_url: str
    handle: str = ""
    title: str = ""
    description_html: str = ""
    description_text: str = ""
    vendor: str | None = None
    sku: str | None = None
    product_type: str | None = None
    tags: list[str] = field(default_factory=list)
    images: list[SourceImage] = field(default_factory=list)
    docs: list[SourceDoc] = field(default_factory=list)
    #: Name → value, from JSON-LD `additionalProperty`, spec tables and
    #: definition lists. Free text on both sides; normalising it is the copy
    #: stage's job, not the scraper's.
    specs: dict[str, str] = field(default_factory=dict)
    #: e.g. {"Size": ["12x24", "24x48"]}. Recorded, but not turned into
    #: Shopify variants — see docs/product-import.md.
    options: dict[str, list[str]] = field(default_factory=dict)
    #: Which extraction step produced each field, so a thin result is
    #: diagnosable without re-running it.
    sources: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "source_url": self.source_url,
            "handle": self.handle,
            "title": self.title,
            "description_html": self.description_html[:60000],
            "description_text": self.description_text[:20000],
            "vendor": self.vendor,
            "sku": self.sku,
            "product_type": self.product_type,
            "tags": self.tags,
            "images": [
                {"url": i.url, "alt": i.alt, "position": i.position}
                for i in self.images
            ],
            "docs": [d.as_dict() for d in self.docs],
            "specs": self.specs,
            "options": self.options,
            "sources": self.sources,
        }


@dataclass
class SourceCollection:
    url: str
    base: str
    title: str | None = None
    description: str | None = None
    platform: str = "other"
    #: Product page URLs in the order the collection listed them.
    product_urls: list[str] = field(default_factory=list)
    #: Whatever the discovery step already knows about each product, keyed by
    #: URL. The Shopify path fills this completely; the HTML path leaves it
    #: empty and the per-product fetch does the work.
    seeds: dict[str, SourceProduct] = field(default_factory=dict)
    pages: int = 0


def client() -> httpx.Client:
    return httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )


# ── URL handling ─────────────────────────────────────────────────────


def split_source_url(url: str) -> tuple[str, str]:
    """`https://x.com/collections/a?b=1` → (`https://x.com`, `/collections/a`).

    The query string is dropped deliberately: a `?page=2` or a theme's
    `?view=grid` pasted out of a browser would otherwise become part of every
    derived URL and quietly import page 2 only.
    """
    raw = (url or "").strip()
    if not raw:
        raise FetchError("No collection URL given.")
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parts = urlsplit(raw)
    if not parts.netloc:
        raise FetchError(f"{url!r} is not a URL I can fetch.")
    base = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    path = parts.path.rstrip("/") or "/"
    return base, path


def absolute(base: str, href: str) -> str | None:
    """Resolve a possibly-relative href, including `//cdn.example.com/x.jpg`."""
    href = (href or "").strip()
    if not href or href.startswith(("data:", "javascript:", "mailto:", "#")):
        return None
    if href.startswith("//"):
        scheme = urlparse(base).scheme or "https"
        return f"{scheme}:{href}"
    return urljoin(base + "/", href)


def same_host(base: str, url: str) -> bool:
    return urlparse(url).netloc.lower() == urlparse(base).netloc.lower()


def _dedupe(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def handle_from_url(url: str) -> str:
    """The last path segment, which on every platform is the product slug."""
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] if path else ""


# ── Small HTML helpers ───────────────────────────────────────────────


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "html.parser")


def html_to_text(html: str, limit: int = 20000) -> str:
    """Readable text out of a description fragment or a whole page."""
    if not html:
        return ""
    soup = soup_of(html)
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", unescape(text))[:limit]


def _meta(soup: BeautifulSoup, *, prop: str = "", name: str = "") -> str | None:
    attrs = {"property": prop} if prop else {"name": name}
    tag = soup.find("meta", attrs=attrs)
    value = (tag.get("content") or "").strip() if tag else ""
    return value or None


def iter_jsonld(soup: BeautifulSoup):
    """Every JSON-LD node on the page, graphs and lists flattened."""
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw.strip())
        except (ValueError, TypeError):
            continue
        yield from _iter_ld_nodes(data)


def _iter_ld_nodes(data):
    if isinstance(data, list):
        for item in data:
            yield from _iter_ld_nodes(item)
    elif isinstance(data, dict):
        if "@graph" in data:
            yield from _iter_ld_nodes(data["@graph"])
        else:
            yield data
            # Nested products live under mainEntity / itemListElement / hasVariant.
            for key in ("mainEntity", "itemListElement", "hasVariant", "item"):
                if key in data:
                    yield from _iter_ld_nodes(data[key])


def _ld_type(node: dict) -> str:
    value = node.get("@type") or ""
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value).strip().lower()


# ── Images ───────────────────────────────────────────────────────────


def _normalize_image_url(url: str) -> str:
    """Strip a CDN's size suffix so two crops of one photo dedupe to one.

    Shopify renders the same image as `shoe_600x.jpg`, `shoe_1024x1024.jpg`
    and `shoe.jpg?v=123`. Importing all three would put the same picture on
    the product three times.
    """
    parts = urlsplit(url)
    path = re.sub(r"_(\d+x\d*|\d*x\d+|small|medium|large|grande|compact|pico)"
                  r"(?=\.[a-zA-Z]{3,4}$)", "", parts.path)
    path = re.sub(r"(?<=\.[a-zA-Z]{3})\.(webp|png|jpg)$", "", path)
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _looks_like_photo(url: str) -> bool:
    if _IMAGE_NOISE.search(url):
        return False
    path = urlparse(url).path.lower()
    if path.endswith(".svg"):  # icons and logos, never product photography
        return False
    return True


#: Keys a gallery entry uses for one photo, largest rendition first. Magento
#: emits all three; taking `full` avoids importing a thumbnail as the product
#: image, which is the failure that looks fine until someone zooms.
_GALLERY_SIZES = ("full", "img", "thumb", "large", "medium", "src", "url")


def _gallery_images(soup: BeautifulSoup, base: str, limit: int) -> list[SourceImage]:
    """Photos from a gallery the page hands to JavaScript as JSON.

    `<img>` scanning cannot see these. Magento puts the real product
    photography in a `text/x-magento-init` block — three renditions of each
    photo, a caption and a position — and leaves the markup with nothing but
    a placeholder div, so a page with six product photos scans as zero.

    Parsed as JSON rather than pattern-matched out of the script text: the
    entries carry which rendition is full size and which is a thumbnail, and
    a regex over URLs throws that away exactly when it matters.
    """
    found: list[SourceImage] = []
    seen: set[str] = set()

    def take(entry: dict) -> None:
        if len(found) >= limit:
            return
        # A video entry names a poster frame; importing that as product
        # photography puts a play button on the product.
        if entry.get("videoUrl") or str(entry.get("type") or "image") != "image":
            return
        raw = next(
            (entry[k] for k in _GALLERY_SIZES
             if isinstance(entry.get(k), str) and entry[k].strip()),
            None,
        )
        if not raw:
            return
        url = absolute(base, raw)
        if not url or not _looks_like_photo(url):
            return
        key = _gallery_key(url)
        if key in seen:
            return
        seen.add(key)
        caption = entry.get("caption")
        found.append(SourceImage(
            url=url,
            alt=(str(caption).strip() or None) if caption else None,
            position=len(found) + 1,
        ))

    def walk(node) -> None:
        if isinstance(node, dict):
            if any(k in node for k in _GALLERY_SIZES[:3]):
                take(node)
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for tag in soup.find_all("script"):
        kind = (tag.get("type") or "").lower()
        if kind not in ("text/x-magento-init", "application/json"):
            continue
        raw = tag.string or tag.get_text() or ""
        if '"thumb"' not in raw and '"full"' not in raw:
            continue
        try:
            walk(json.loads(raw))
        except ValueError:
            continue  # A script that isn't valid JSON is not a gallery.
    return found


def _gallery_key(url: str) -> str:
    """Two renditions of one photo, reduced to the same key.

    Magento's cache path is `/media/catalog/product/cache/<hash>/3/d/name.jpg`
    where the hash encodes the size, so the same photograph appears under
    three different URLs that `_normalize_image_url` has no reason to
    consider equal — it strips size *suffixes*, and here the size is a
    directory. The filename is what identifies the photo.
    """
    path = urlparse(_normalize_image_url(url)).path
    return path.rsplit("/", 1)[-1].lower() or path.lower()


def collect_images(soup: BeautifulSoup, base: str, limit: int = 20) -> list[SourceImage]:
    """Product photography from the page, best guess first.

    A JSON gallery is asked first and, when it answers, is the whole answer:
    it is the site stating which photographs are the product's, in order,
    with captions. Scanning `<img>` alongside it would only add the page's
    furniture back in.

    Otherwise reads `<img src>` plus the lazy-loading attributes themes use
    instead (`data-src`, `data-original`, `srcset`), because a page whose
    images only appear after JavaScript runs still names them in the markup.
    """
    from_gallery = _gallery_images(soup, base, limit)
    if from_gallery:
        return from_gallery

    found: list[SourceImage] = []
    seen: set[str] = set()

    def add(raw: str | None, alt: str | None) -> None:
        if not raw or len(found) >= limit:
            return
        url = absolute(base, raw)
        if not url or not _looks_like_photo(url):
            return
        key = _normalize_image_url(url)
        if key in seen:
            return
        seen.add(key)
        found.append(SourceImage(url=url, alt=(alt or "").strip() or None,
                                 position=len(found) + 1))

    for tag in soup.find_all("img"):
        alt = tag.get("alt")
        src = (
            tag.get("src") or tag.get("data-src") or tag.get("data-original")
            or tag.get("data-lazy") or tag.get("data-image")
        )
        if not src and tag.get("srcset"):
            # Largest candidate in the srcset — the last entry, conventionally.
            candidates = [c.strip().split(" ")[0] for c in tag["srcset"].split(",")]
            src = candidates[-1] if candidates else None
        add(src, alt)

    return found


# ── Documents ────────────────────────────────────────────────────────


def classify_doc(url: str, text: str = "") -> str:
    """What kind of document this is, from its link text and filename."""
    blob = f"{text} {urlparse(url).path}".lower()
    if any(k in blob for k in ("warranty", "guarantee")):
        return "warranty"
    if any(k in blob for k in ("install", "fitting", "subfloor", "submittal")):
        return "installation"
    if any(k in blob for k in ("maintenance", "care", "cleaning")):
        return "maintenance"
    if any(k in blob for k in ("sds", "msds", "safety", "declaration",
                               "certificate", "emission")):
        return "compliance"
    if any(k in blob for k in ("brochure", "catalog", "catalogue", "lookbook")):
        return "brochure"
    if any(k in blob for k in ("spec", "tech", "data sheet", "datasheet",
                               "tds", "cut sheet")):
        return "spec"
    return "other"


def collect_docs(soup: BeautifulSoup, base: str, limit: int = 12) -> list[SourceDoc]:
    """Documentation links: anything ending in a document extension, plus
    anchors whose text says it's a spec sheet even when the href doesn't."""
    docs: list[SourceDoc] = []
    seen: set[str] = set()
    for tag in soup.find_all("a", href=True):
        if len(docs) >= limit:
            break
        url = absolute(base, tag["href"])
        if not url:
            continue
        text = re.sub(r"\s+", " ", tag.get_text(" ", strip=True))[:200]
        path = urlparse(url).path.lower()
        by_extension = path.endswith(DOC_EXTENSIONS)
        by_text = (
            any(m in text.lower() for m in DOC_TEXT_MARKERS)
            and ("download" in url.lower() or "/file" in path or ".pdf" in url.lower())
        )
        if not (by_extension or by_text):
            continue
        key = url.split("?", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        title = text or path.rsplit("/", 1)[-1]
        docs.append(SourceDoc(url=url, title=title[:200],
                              kind=classify_doc(url, text)))
    return docs


# ── Specifications ───────────────────────────────────────────────────

#: A spec name is short and label-like. Anything longer is prose that
#: happened to land in a table cell.
_SPEC_KEY_MAX = 60
_SPEC_VALUE_MAX = 300


def _clean_cell(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip(" : ")


def collect_specs(soup: BeautifulSoup, limit: int = 40) -> dict[str, str]:
    """Name/value pairs out of two-column tables and definition lists.

    Both shapes are how a manufacturer publishes "Thickness: 20 mil" for
    humans, and both are trivially machine-readable — which makes this worth
    doing properly rather than leaving to the model to dig out of prose.
    """
    specs: dict[str, str] = {}

    def put(key: str, value: str) -> None:
        key, value = key.strip(), value.strip()
        if not key or not value or len(key) > _SPEC_KEY_MAX:
            return
        if key.lower() == value.lower() or len(specs) >= limit:
            return
        specs.setdefault(key, value[:_SPEC_VALUE_MAX])

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) == 2:
                put(_clean_cell(cells[0]), _clean_cell(cells[1]))

    for dl in soup.find_all("dl"):
        terms = dl.find_all("dt")
        definitions = dl.find_all("dd")
        for term, definition in zip(terms, definitions):
            put(_clean_cell(term), _clean_cell(definition))

    return specs


def _specs_from_ld(node: dict) -> dict[str, str]:
    props = node.get("additionalProperty") or []
    if isinstance(props, dict):
        props = [props]
    out: dict[str, str] = {}
    for prop in props:
        if not isinstance(prop, dict):
            continue
        name = str(prop.get("name") or "").strip()
        value = prop.get("value")
        if isinstance(value, dict):
            value = value.get("name") or value.get("value")
        value = str(value or "").strip()
        if name and value and len(name) <= _SPEC_KEY_MAX:
            out.setdefault(name, value[:_SPEC_VALUE_MAX])
    return out


# ── Shopify sources ──────────────────────────────────────────────────


def _product_from_shopify_node(node: dict, base: str) -> SourceProduct:
    handle = str(node.get("handle") or "").strip()
    body = node.get("body_html") or ""
    product = SourceProduct(
        source_url=f"{base}/products/{handle}" if handle else base,
        handle=handle,
        title=(node.get("title") or handle or "").strip(),
        description_html=body,
        description_text=html_to_text(body),
        vendor=(node.get("vendor") or None),
        product_type=(node.get("product_type") or None),
        tags=[t.strip() for t in (node.get("tags") or []) if str(t).strip()],
    )
    product.sources.update(
        {"title": "shopify-json", "description": "shopify-json",
         "images": "shopify-json"}
    )
    for index, image in enumerate(node.get("images") or [], start=1):
        src = absolute(base, str(image.get("src") or ""))
        if src:
            product.images.append(
                SourceImage(url=src, alt=(image.get("alt") or None), position=index)
            )
    for option in node.get("options") or []:
        name = str(option.get("name") or "").strip()
        values = [str(v).strip() for v in (option.get("values") or []) if str(v).strip()]
        # Shopify gives every product a "Title: Default Title" option; it
        # describes the absence of options, not an option.
        if name and values and values != ["Default Title"]:
            product.options[name] = values
    variants = node.get("variants") or []
    if variants:
        sku = str(variants[0].get("sku") or "").strip()
        product.sku = sku or None
    # Docs and specs live in the body HTML on a Shopify source; the per-
    # product page fetch may find more, and merges into this.
    if body:
        body_soup = soup_of(body)
        product.docs = collect_docs(body_soup, base)
        product.specs = collect_specs(body_soup)
    return product


def fetch_shopify_collection(
    base: str, path: str, http: httpx.Client, *, max_products: int
) -> tuple[list[SourceProduct], int] | None:
    """The collection's products from Shopify's own JSON, or None if this
    isn't a Shopify collection URL."""
    if "/collections/" not in path:
        return None
    endpoint = f"{base}{path}/products.json"
    if not may_fetch(base, f"{path}/products.json", client=http):
        return None
    products: list[SourceProduct] = []
    pages = 0
    for page in range(1, MAX_COLLECTION_PAGES + 1):
        try:
            resp = http.get(endpoint, params={"limit": 250, "page": page})
        except Exception as e:  # noqa: BLE001 - fall through to the HTML path
            log.info("shopify collection JSON failed for %s: %s", endpoint, e)
            return None if page == 1 else (products, pages)
        if resp.status_code != 200:
            return None if page == 1 else (products, pages)
        try:
            payload = resp.json() or {}
        except ValueError:
            return None if page == 1 else (products, pages)
        nodes = payload.get("products")
        if nodes is None:
            return None if page == 1 else (products, pages)
        pages += 1
        for node in nodes:
            product = _product_from_shopify_node(node, base)
            if product.handle:
                products.append(product)
            if len(products) >= max_products:
                return products, pages
        if len(nodes) < 250:
            break
        time.sleep(PAUSE)
    return products, pages


# ── Generic HTML collection ──────────────────────────────────────────

#: What a product URL looks like across Shopify, Woo, BigCommerce, Magento
#: and hand-rolled sites.
_PRODUCT_PATH = re.compile(r"/(products?|shop|item|p|tile|tiles|collections?/[^/]+/products)/[^/]+$", re.I)


#: Containers a storefront wraps each product card in. Checked before the
#: URL pattern below because they carry information the URL doesn't: the
#: page has already declared "this is a product", so the link inside needs
#: no prefix to be recognised as one.
_ITEM_SELECTORS = (
    ".product-item",        # Magento, and most themes that copied it
    ".product-card",
    ".product-tile",
    "li.item.product",
    "[data-container='product-grid']",
)


def _grid_product_links(soup: BeautifulSoup, base: str, page_url: str) -> list[str]:
    """Product URLs taken from the grid's own markup rather than their shape.

    `_product_links` below can only recognise a product by its path, which
    means a store that doesn't prefix them is invisible to it — Magento puts
    products at the site root (`/3dbbemg510`), and no pattern that matches
    that could avoid also matching `/about` and `/contact`.

    The grid answers the question directly. A card in `.product-item` is a
    product because the page says so, and the link inside it needs no prefix
    to be trusted. That also keeps the page's own furniture out: a "Download
    the catalogue" link sitting in the category description is a bare path
    like the products are, and only its position tells you it isn't one.

    Within a card, the first same-host link that isn't a fragment wins.
    Wishlist and compare controls are anchors too, but they point at the
    collection page with a `#` on the end — which, once the fragment is
    stripped, is the page we're already on.
    """
    return [card["url"] for card in _grid_cards(soup, base, page_url)]


def _grid_cards(soup: BeautifulSoup, base: str, page_url: str) -> list[dict]:
    """Each product card in the grid: its link, and the picture beside it.

    The picture matters as much as the link. A manufacturer's product page
    can fail to yield any photograph — a template without a gallery, a
    render that didn't happen — and the range page has been showing one for
    that product the whole time. Throwing it away at discovery meant a
    product arriving in the store with no image while its thumbnail sat one
    click away in the collection we had just read.
    """
    here = page_url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    cards: list[dict] = []
    seen: set[str] = set()
    for selector in _ITEM_SELECTORS:
        try:
            elements = soup.select(selector)
        except Exception:  # noqa: BLE001 - a bad selector must not kill a run
            continue
        for element in elements:
            found = None
            for tag in element.find_all("a", href=True):
                href = tag["href"]
                if "#" in href:
                    continue
                url = absolute(base, href)
                if not url or not same_host(base, url):
                    continue
                clean = url.split("?", 1)[0].split("#", 1)[0]
                if clean.rstrip("/") == here or clean in seen:
                    continue
                found = clean
                break
            if not found:
                continue
            seen.add(found)
            cards.append({
                "url": found,
                "title": _card_title(element, found),
                "image": _card_image(element, base),
            })
        if cards:
            # A page whose cards were found under one selector shouldn't have
            # a second, looser one bolted on top of the result.
            break
    return cards


#: Where a grid card names its product, best evidence first. These are the
#: elements a storefront uses to render the name a shopper reads — Magento's
#: `product-item-name`/`product-item-link` and the `itemprop` the same markup
#: carries for search engines.
_CARD_NAME_SELECTORS = (
    "[itemprop='name']",
    ".product-item-name",
    ".product-item-link",
    ".product-name",
    ".card-title",
    "h2", "h3", "h4",
)


def _card_title(element, product_url: str | None = None) -> str | None:
    """The product's name as the range page gives it.

    Ordered by what each candidate actually is. The name element is the
    site's own statement of what this product is called. The anchor pointing
    at the product is the same claim, one level less explicit. The image's
    `alt` is a description of a *photograph*, which is only the product's
    name on a grid that shows nothing else — and on some it is the SKU, or
    the filename.

    The alt used to come first, to dodge a real problem: a card's anchors
    include "Add to Favorites" and "Add to Compare", so the first link's
    text is often neither the name nor anything like it. The fix for that is
    to ask which anchor points at the product, not to stop reading anchors.

    It matters more than it looks. The whole store title is composed from
    this — the size and the variant are both read out of it — so a card that
    yields the SKU instead of "Advantage | 24\"x 48\" Graphite Matte" costs
    the size and the colour, and two products that lose their size compose
    one name between them.
    """
    for selector in _CARD_NAME_SELECTORS:
        try:
            node = element.select_one(selector)
        except Exception:  # noqa: BLE001 - a bad selector must not kill a run
            continue
        if node:
            text = node.get_text(" ", strip=True)
            if text:
                return text[:600]

    if product_url:
        for tag in element.find_all("a", href=True):
            if tag["href"].split("?", 1)[0].split("#", 1)[0].rstrip("/") not in (
                product_url, product_url.rstrip("/")
            ) and not product_url.endswith(tag["href"].rstrip("/")):
                continue
            text = tag.get_text(" ", strip=True)
            if text:
                return text[:600]

    image = element.find("img", alt=True)
    if image and image["alt"].strip():
        return image["alt"].strip()[:600]
    link = element.find("a", href=True)
    text = link.get_text(" ", strip=True) if link else ""
    return text[:600] or None


def _card_image(element, base: str) -> str | None:
    for tag in element.find_all("img"):
        raw = (
            tag.get("src") or tag.get("data-src") or tag.get("data-original")
            or tag.get("data-lazy") or tag.get("data-image")
        )
        url = absolute(base, raw) if raw else None
        if url and _looks_like_photo(url):
            return url
    return None


def _product_links(soup: BeautifulSoup, base: str) -> list[str]:
    urls: list[str] = []
    for tag in soup.find_all("a", href=True):
        url = absolute(base, tag["href"])
        if not url or not same_host(base, url):
            continue
        clean = url.split("?", 1)[0].split("#", 1)[0]
        if _PRODUCT_PATH.search(urlparse(clean).path):
            urls.append(clean)
    return _dedupe(urls)


def _seed_from_grid(
    out: SourceCollection, soup: BeautifulSoup, base: str, page_url: str
) -> None:
    """Keep each card's picture and name against its URL.

    The Shopify path gets seeds from `products.json` for free. Everything
    else had none, so the thumbnail the range page was already showing was
    read, used to find the link, and thrown away — and a product whose own
    page yields no photograph then arrived in the store with no image at
    all, one click from the picture we had just looked at.
    """
    for card in _grid_cards(soup, base, page_url):
        if card["url"] in out.seeds or not (card["image"] or card["title"]):
            continue
        seed = SourceProduct(
            source_url=card["url"], handle=handle_from_url(card["url"])
        )
        if card["title"]:
            seed.title = card["title"]
            # Recorded, because it is the weakest title in the pipeline and
            # the product's own page may do better — see `_merge_page`.
            seed.sources["title"] = "collection-grid"
        if card["image"]:
            seed.images = [SourceImage(url=card["image"], position=1)]
            seed.sources["images"] = "collection-grid"
        out.seeds[card["url"]] = seed


def _extract_products(soup: BeautifulSoup, base: str, page_url: str) -> list[str]:
    """The three ways to name a collection's products, best evidence first.

    Structured data is a statement of fact by the site, the grid is the
    site's own layout, and the URL pattern is us guessing from a string.
    Ordered accordingly, and each is tried only when the one above found
    nothing — a page with good JSON-LD shouldn't also get a regex opinion.
    """
    return (
        _ld_collection_urls(soup, base)
        or _grid_product_links(soup, base, page_url)
        or _product_links(soup, base)
    )


#: Query parameters storefronts page with, in the order they're guessed.
#: `page` first because it's the common one; `p` is Magento's.
_PAGE_PARAMS = ("page", "p")


def _page_param(soup: BeautifulSoup, base: str, path: str) -> str:
    """Which query parameter this site pages with, read off its own links.

    Guessing wrong is quiet rather than loud, which is what makes it worth
    reading instead: Magento answers `?page=2` with HTTP 200 and the *first*
    page, so the caller sees a successful fetch naming no new products and
    concludes the collection ended. A 13-product collection imports 12 and
    reports success.

    So the pagination links already on the page are asked first, and the
    guess is only a fallback for a page that has none.
    """
    for tag in soup.find_all("a", href=True):
        href = absolute(base, tag["href"])
        if not href or not same_host(base, href):
            continue
        parsed = urlparse(href)
        if parsed.path.rstrip("/") != path.rstrip("/"):
            continue
        query = parse_qs(parsed.query)
        for candidate in _PAGE_PARAMS:
            values = query.get(candidate) or []
            if any(v.isdigit() and int(v) > 1 for v in values):
                return candidate
    return _PAGE_PARAMS[0]


def _ld_collection_urls(soup: BeautifulSoup, base: str) -> list[str]:
    """Product URLs from an `ItemList`, which is how a well-marked-up
    category page names its members."""
    urls: list[str] = []
    for node in iter_jsonld(soup):
        if _ld_type(node) == "listitem":
            item = node.get("item")
            target = item.get("url") if isinstance(item, dict) else node.get("url")
            if target:
                resolved = absolute(base, str(target))
                if resolved:
                    urls.append(resolved.split("?", 1)[0])
        elif _ld_type(node) in _LD_TYPES_PRODUCT and node.get("url"):
            resolved = absolute(base, str(node["url"]))
            if resolved:
                urls.append(resolved.split("?", 1)[0])
    return _dedupe(urls)


def _collection_meta(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    title = None
    heading = soup.find("h1")
    if heading:
        title = _clean_cell(heading) or None
    title = title or _meta(soup, prop="og:title") or (
        soup.title.get_text(strip=True) if soup.title else None
    )
    description = (
        _meta(soup, name="description") or _meta(soup, prop="og:description")
    )
    for node in iter_jsonld(soup):
        if _ld_type(node) in {"collectionpage", "itemlist"}:
            title = title or str(node.get("name") or "") or None
            description = description or str(node.get("description") or "") or None
    return title, description


def discover_collection(
    url: str, *, max_products: int = 200, http: httpx.Client | None = None
) -> SourceCollection:
    """Everything the collection page will tell us in one pass.

    Raises `FetchError` with something the owner can act on — a robots
    refusal, a 404, a page that lists no products — rather than returning an
    empty result that looks like a collection with nothing in it.
    """
    base, path = split_source_url(url)
    own = http is None
    http = http or client()
    out = SourceCollection(url=f"{base}{path}", base=base)
    try:
        if not may_fetch(base, path, client=http):
            raise FetchError(
                f"robots.txt on {base} disallows {path}. Not fetched — that's "
                "the site's decision to make."
            )

        shopify = fetch_shopify_collection(base, path, http, max_products=max_products)
        if shopify is not None and shopify[0]:
            products, pages = shopify
            out.platform = "shopify"
            out.pages = pages
            for product in products:
                out.product_urls.append(product.source_url)
                out.seeds[product.source_url] = product
            log.info("shopify collection %s: %d products", url, len(products))

        # The collection page itself, for its title and description — needed
        # on the Shopify path too, since products.json says nothing about the
        # collection it came from.
        page_html, page_error = _fetch_html(http, f"{base}{path}")
        if page_html:
            out.pages += 1
            soup = soup_of(page_html)
            out.title, out.description = _collection_meta(soup)
            if not out.product_urls:
                page_url = f"{base}{path}"
                found = _extract_products(soup, base, page_url)
                out.product_urls.extend(found)
                _seed_from_grid(out, soup, base, page_url)
                # Paginate while a page keeps naming products we haven't seen.
                param = _page_param(soup, base, path)
                for page in range(2, MAX_COLLECTION_PAGES + 1):
                    if len(out.product_urls) >= max_products:
                        break
                    time.sleep(PAUSE)
                    more_html = _get_html(http, page_url, params={param: page})
                    if not more_html:
                        break
                    out.pages += 1
                    more_soup = soup_of(more_html)
                    more = _extract_products(more_soup, base, page_url)
                    _seed_from_grid(out, more_soup, base, page_url)
                    fresh = [u for u in more if u not in set(out.product_urls)]
                    if not fresh:
                        break
                    out.product_urls.extend(fresh)
                out.product_urls = _dedupe(out.product_urls)[:max_products]

        render_note = ""
        if not out.product_urls:
            # Either the plain GET never landed, or it landed and named no
            # products — the second is the signature of a grid JavaScript
            # fills in after load. One Firecrawl render is worth trying
            # before giving up either way.
            rendered_html, render_error = _firecrawl_scrape(f"{base}{path}")
            if rendered_html:
                out.pages += 1
                soup = soup_of(rendered_html)
                if not out.title:
                    out.title, out.description = _collection_meta(soup)
                found = _extract_products(soup, base, f"{base}{path}")
                out.product_urls = _dedupe(found)[:max_products]
                _seed_from_grid(out, soup, base, f"{base}{path}")
                if out.product_urls:
                    page_error = None
            elif render_error and not render_error.startswith("Firecrawl isn't configured"):
                # Recorded as a note, never folded into `page_error`: whether
                # the page itself was readable is what picks the message
                # below, and a failed *render* says nothing about that. Losing
                # that distinction turned "nothing here looked like a product"
                # into "couldn't reach the site, check your firewall" for a
                # page that had in fact loaded perfectly — which is the exact
                # wrong-diagnosis trap the next test down already documents.
                render_note = (
                    f" Rendering it with JavaScript was tried too, and failed: "
                    f"{render_error}"
                )

        if not out.product_urls:
            if page_error:
                # Never reached the page at all. Saying "no products here"
                # would send someone off to inspect markup that was never
                # read — the fix is a network, DNS, or blocked-request one.
                raise FetchError(
                    f"Couldn't read {out.url} — {page_error}. Nothing was "
                    "imported because nothing was fetched: check the URL "
                    "opens in a browser, and that this machine can reach the "
                    "site (a proxy, a firewall, or the site blocking "
                    f"non-browser requests would all look like this).{render_note}"
                )
            raise FetchError(
                f"No products found at {out.url}. The page loaded, but nothing "
                "on it looked like a product link — if the collection renders "
                f"its grid with JavaScript, this can't see it.{render_note}"
            )
        out.product_urls = out.product_urls[:max_products]
        out.seeds = {u: s for u, s in out.seeds.items() if u in set(out.product_urls)}
        return out
    finally:
        if own:
            http.close()


def _fetch_html(
    http: httpx.Client, url: str, params: dict | None = None
) -> tuple[str | None, str | None]:
    """The page, or None and *why* — the reason is the whole point.

    "The site didn't answer" and "the page had nothing on it" are different
    problems with different fixes, and a caller that only sees None has to
    guess which one it hit. Guessing wrong produces the worst kind of error
    message: one that confidently describes something that didn't happen.
    """
    try:
        resp = http.get(url, params=params)
    except Exception as e:  # noqa: BLE001 - an unreadable page is not a crash
        log.info("fetch failed for %s: %s", url, e)
        return None, f"{type(e).__name__}: {e}"[:300]
    if resp.status_code != 200:
        return None, f"the site answered HTTP {resp.status_code}"
    content_type = resp.headers.get("content-type", "")
    if "html" not in content_type and not resp.text.lstrip().startswith("<"):
        return None, f"the response wasn't HTML (content-type {content_type!r})"
    return resp.text, None


def _get_html(http: httpx.Client, url: str, params: dict | None = None) -> str | None:
    return _fetch_html(http, url, params)[0]


def _firecrawl_scrape(url: str) -> tuple[str | None, str | None]:
    """`url`, rendered by Firecrawl's own browser, or None and why.

    The fallback for a collection whose plain GET comes back with nothing to
    parse — usually a grid a theme fills in with JavaScript after load, which
    `httpx` can never see. Firecrawl runs a real browser against the page and
    hands back the HTML that produces, so it's tried only once a direct fetch
    has already failed to find products, not on every request.
    """
    from dashboard.config import get_settings

    settings = get_settings()
    if not settings.has_firecrawl:
        return None, "Firecrawl isn't configured (set FIRECRAWL_API_KEY)."
    try:
        resp = httpx.post(
            f"{FIRECRAWL_BASE_URL}/scrape",
            headers={"Authorization": f"Bearer {settings.firecrawl_api_key}"},
            json={"url": url, "formats": ["html"], "waitFor": 2000},
            # Deliberately well under the 60s a Vercel function gets: this
            # call is one step of a discovery pass, not the whole budget, and
            # a render that hasn't answered in 25s is not going to rescue the
            # pass — it's going to take the function down with it and lose the
            # run's progress. Failing here is recoverable; being killed isn't.
            timeout=25.0,
        )
    except Exception as e:  # noqa: BLE001 - report it, don't crash the import
        return None, f"Firecrawl request failed: {type(e).__name__}: {e}"[:300]
    if resp.status_code != 200:
        return None, f"Firecrawl answered HTTP {resp.status_code}"
    try:
        payload = resp.json()
    except ValueError:
        return None, "Firecrawl returned a non-JSON response"
    if not payload.get("success"):
        return None, str(payload.get("error") or "Firecrawl reported failure")[:300]
    html = ((payload.get("data") or {}).get("html")) or ""
    if not html:
        return None, "Firecrawl returned no HTML"
    return html, None


# ── One product page ─────────────────────────────────────────────────


def fetch_product(
    url: str,
    base: str,
    *,
    seed: SourceProduct | None = None,
    http: httpx.Client | None = None,
) -> SourceProduct:
    """A product record for `url`, merging the page into whatever `seed` has.

    The seed is what the collection feed already told us (the Shopify path
    fills it completely). The page is fetched regardless, because that is
    where the spec sheets are: a manufacturer links their PDFs from a
    Downloads tab the theme renders, which `body_html` never sees.
    """
    own = http is None
    http = http or client()
    product = seed or SourceProduct(source_url=url, handle=handle_from_url(url))
    product.source_url = url
    try:
        path = urlparse(url).path or "/"
        if not may_fetch(base, path, client=http):
            product.sources["page"] = "robots-disallowed"
            if not product.title:
                raise FetchError(f"robots.txt disallows {path}.")
            return product

        html = _get_html(http, url)
        if not html:
            product.sources["page"] = "unreadable"
            if not product.title:
                raise FetchError(f"Could not read the product page at {url}.")
            return product

        soup = soup_of(html)
        _merge_page(product, soup, base)
        return product
    finally:
        if own:
            http.close()


def _merge_page(product: SourceProduct, soup: BeautifulSoup, base: str) -> None:
    """Fold a fetched product page into `product`, filling gaps only.

    Gaps only, and in that order, because the seed came from a structured
    feed and the page is guesswork by comparison. The exception is documents
    and specs, which are additive: finding a warranty PDF on the page when
    the feed had a spec sheet should give the product both.
    """
    ld_product: dict = {}
    for node in iter_jsonld(soup):
        if _ld_type(node) in _LD_TYPES_PRODUCT and node.get("name"):
            ld_product = node
            break

    # A grid card's title is the one seed field the product's own page can
    # beat. Everything else in a seed came from a structured feed, which is
    # better evidence than markup; a card title is markup too, and thinner —
    # often a photograph's alt text, sometimes the SKU. Where the product
    # page states its own name in JSON-LD, that is the site saying what this
    # product is called, and it wins.
    #
    # It has to win, because the store's whole title is composed out of this
    # one string: the size and the variant are both read from it. A card that
    # yields "ADGAM2448" costs both, and two products that lose their size
    # compose one name between them — which is a product that never reaches
    # the store.
    from_grid = product.sources.get("title") == "collection-grid"
    ld_name = str(ld_product.get("name") or "").strip()
    if from_grid and ld_name and ld_name != product.title:
        product.title = ld_name[:600]
        product.sources["title"] = "json-ld"
    elif not product.title:
        title = (
            ld_name
            or _meta(soup, prop="og:title")
            or (_clean_cell(soup.find("h1")) if soup.find("h1") else "")
        )
        if title:
            product.title = title[:600]
            product.sources["title"] = "json-ld" if ld_name else "html"

    if not product.description_html:
        described = str(ld_product.get("description") or "").strip()
        if described:
            product.description_text = described[:20000]
            product.description_html = f"<p>{unescape(described)}</p>"
            product.sources["description"] = "json-ld"
        else:
            fragment = _description_fragment(soup)
            if fragment:
                product.description_html = fragment
                product.description_text = html_to_text(fragment)
                product.sources["description"] = "html"
            else:
                og = _meta(soup, prop="og:description") or _meta(soup, name="description")
                if og:
                    product.description_text = og
                    product.description_html = f"<p>{unescape(og)}</p>"
                    product.sources["description"] = "opengraph"

    if not product.vendor:
        brand = ld_product.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")
        if brand:
            product.vendor = str(brand)[:200]
            product.sources["vendor"] = "json-ld"

    if not product.sku and ld_product.get("sku"):
        product.sku = str(ld_product["sku"])[:100]

    # Images are additive when the page states them structurally, and a
    # fallback otherwise. The distinction is what lets a collection-grid
    # thumbnail be the cover *and* the product page's own gallery follow it:
    # a gap-only rule would see the seed's one image and skip the gallery
    # entirely, which is how "two photographs" became "the thumbnail".
    #
    # Loose `<img>` scanning stays gap-only. On a feed-seeded product it
    # would append the site's furniture to a set that was already right.
    structured = _ld_images(ld_product, base) or _gallery_images(soup, base, 20)
    if structured:
        known = {_gallery_key(i.url) for i in product.images}
        for image in structured:
            key = _gallery_key(image.url)
            if key in known:
                continue
            known.add(key)
            product.images.append(image)
        product.sources.setdefault(
            "images", "json-ld" if _ld_images(ld_product, base) else "gallery"
        )
    elif not product.images:
        og_image = _meta(soup, prop="og:image")
        gathered = collect_images(soup, base)
        if og_image:
            resolved = absolute(base, og_image)
            if resolved and all(
                _normalize_image_url(resolved) != _normalize_image_url(i.url)
                for i in gathered
            ):
                gathered.insert(0, SourceImage(url=resolved, position=0))
        if gathered:
            product.images = gathered
            product.sources["images"] = "html"

    for position, image in enumerate(product.images, start=1):
        image.position = position

    # Additive: the page's documents and specs join the feed's.
    known_docs = {d.url.split("?", 1)[0] for d in product.docs}
    for doc in collect_docs(soup, base):
        if doc.url.split("?", 1)[0] not in known_docs:
            product.docs.append(doc)
            known_docs.add(doc.url.split("?", 1)[0])

    for name, value in {**_specs_from_ld(ld_product), **collect_specs(soup)}.items():
        product.specs.setdefault(name, value)

    product.sources.setdefault("page", "fetched")


def _ld_images(node: dict, base: str) -> list[SourceImage]:
    raw = node.get("image")
    if isinstance(raw, (str, dict)):
        raw = [raw]
    images: list[SourceImage] = []
    for index, entry in enumerate(raw or [], start=1):
        if isinstance(entry, dict):
            entry = entry.get("url") or entry.get("contentUrl")
        url = absolute(base, str(entry or ""))
        if url and _looks_like_photo(url):
            images.append(SourceImage(url=url, position=index))
    return images


#: Where a theme puts the description when it isn't in JSON-LD. Ordered most
#: specific first — a match on `.product__description` is the description; a
#: match on `[itemprop=description]` might be the whole page.
_DESCRIPTION_SELECTORS = (
    ".product__description", ".product-single__description",
    ".product-description", "#product-description", ".product__text",
    ".woocommerce-product-details__short-description", "#tab-description",
    "[itemprop='description']", ".rte",
)


def _description_fragment(soup: BeautifulSoup) -> str:
    for selector in _DESCRIPTION_SELECTORS:
        node = soup.select_one(selector)
        if node:
            text = node.get_text(" ", strip=True)
            if len(text) >= 40:  # a heading or a stray label isn't a description
                for tag in node(["script", "style", "noscript"]):
                    tag.decompose()
                return str(node)[:60000]
    return ""
