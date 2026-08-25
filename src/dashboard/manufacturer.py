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
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

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


def collect_images(soup: BeautifulSoup, base: str, limit: int = 20) -> list[SourceImage]:
    """Product photography from the page, best guess first.

    Reads `<img src>` plus the lazy-loading attributes themes use instead
    (`data-src`, `data-original`, `srcset`), because a page whose images only
    appear after JavaScript runs still names them in the markup.
    """
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
        page_html = _get_html(http, f"{base}{path}")
        if page_html:
            out.pages += 1
            soup = soup_of(page_html)
            out.title, out.description = _collection_meta(soup)
            if not out.product_urls:
                found = _ld_collection_urls(soup, base) or _product_links(soup, base)
                out.product_urls.extend(found)
                # Paginate while a page keeps naming products we haven't seen.
                for page in range(2, MAX_COLLECTION_PAGES + 1):
                    if len(out.product_urls) >= max_products:
                        break
                    time.sleep(PAUSE)
                    more_html = _get_html(http, f"{base}{path}", params={"page": page})
                    if not more_html:
                        break
                    out.pages += 1
                    more_soup = soup_of(more_html)
                    more = (
                        _ld_collection_urls(more_soup, base)
                        or _product_links(more_soup, base)
                    )
                    fresh = [u for u in more if u not in set(out.product_urls)]
                    if not fresh:
                        break
                    out.product_urls.extend(fresh)
                out.product_urls = _dedupe(out.product_urls)[:max_products]

        if not out.product_urls:
            raise FetchError(
                f"No products found at {out.url}. The page loaded, but nothing "
                "on it looked like a product link — if the collection renders "
                "its grid with JavaScript, this can't see it."
            )
        out.product_urls = out.product_urls[:max_products]
        out.seeds = {u: s for u, s in out.seeds.items() if u in set(out.product_urls)}
        return out
    finally:
        if own:
            http.close()


def _get_html(http: httpx.Client, url: str, params: dict | None = None) -> str | None:
    try:
        resp = http.get(url, params=params)
    except Exception as e:  # noqa: BLE001 - an unreadable page is not a crash
        log.info("fetch failed for %s: %s", url, e)
        return None
    if resp.status_code != 200:
        return None
    content_type = resp.headers.get("content-type", "")
    if "html" not in content_type and not resp.text.lstrip().startswith("<"):
        return None
    return resp.text


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

    if not product.title:
        title = (
            str(ld_product.get("name") or "").strip()
            or _meta(soup, prop="og:title")
            or (_clean_cell(soup.find("h1")) if soup.find("h1") else "")
        )
        if title:
            product.title = title[:600]
            product.sources["title"] = "json-ld" if ld_product.get("name") else "html"

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

    if not product.images:
        images = _ld_images(ld_product, base)
        if images:
            product.images = images
            product.sources["images"] = "json-ld"
        else:
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
                for position, image in enumerate(gathered, start=1):
                    image.position = position
                product.images = gathered
                product.sources["images"] = "html"

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
