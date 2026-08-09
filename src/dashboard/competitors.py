"""Reading a competitor's public site.

Everything here fetches pages any browser can fetch, at a browsing pace, and
parses what the site publishes for machines. No login, no paid API, no
attempt to get at anything a site hasn't chosen to expose.

The order of preference is the whole design:

  1. **Shopify's own JSON.** Most local flooring retailers run Shopify, and
     Shopify publishes the entire catalogue at `/products.json` and the blog
     as Atom. That's a documented, stable, paginated feed — no HTML parsing,
     nothing that breaks when they change themes.
  2. **JSON-LD.** Sites that aren't Shopify usually still emit
     `schema.org/Product` for Google's benefit. Same data, more work.
  3. **Nothing.** Recorded as `platform=other` and left alone rather than
     retried nightly as though it might start working.

Politeness is not decoration here. `_PAUSE` between requests, a real
identifying User-Agent, and a page cap per run — a competitor's server
shouldn't be able to tell the difference between this and one person
browsing, and a small business's site shouldn't be the thing that pays for
our dashboard being thorough.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape

import httpx

log = logging.getLogger(__name__)

#: Seconds between requests to the same host. A human clicking through a
#: catalogue is slower than this.
_PAUSE = 1.0
#: Pages of 250 products. 40 covers a 10,000-product catalogue, which is far
#: larger than any local flooring retailer, and stops a misconfigured URL
#: from paging forever.
_MAX_PAGES = 40
_TIMEOUT = 20.0

#: Says what this is and who it's for. A competitor who wants to block it can
#: block it — that's their call to make, and hiding would be the wrong answer
#: to their having made it.
USER_AGENT = (
    "DRFlooringControlCenter/1.0 (+https://drflooring.ca; "
    "competitor price monitoring; contact via drflooring.ca)"
)


class FetchError(RuntimeError):
    """The site couldn't be read. Carries a message meant for the owner."""


@dataclass
class RawProduct:
    handle: str
    title: str
    external_id: str | None = None
    vendor: str | None = None
    product_type: str | None = None
    url: str | None = None
    price_min: float = 0.0
    price_max: float = 0.0
    currency: str | None = None
    available: bool | None = None
    published_at: datetime | None = None


@dataclass
class RawPost:
    url: str
    title: str
    summary: str | None = None
    author: str | None = None
    published_at: datetime | None = None


@dataclass
class Fetched:
    products: list[RawProduct] = field(default_factory=list)
    posts: list[RawPost] = field(default_factory=list)
    pages: int = 0


def normalize_base(url: str) -> str:
    """`drflooring.ca` or `http://drflooring.ca/` → `https://drflooring.ca`."""
    raw = (url or "").strip().rstrip("/")
    if not raw:
        raise FetchError("No URL set for this competitor.")
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )


def _parse_dt(value: str | None) -> datetime | None:
    """Shopify hands back ISO 8601 with an offset; Atom does the same.

    Returned naive-UTC to match every other datetime column in this schema
    (SQLite has no tz type and the columns are plain DateTime, so storing an
    aware value here would compare wrongly against the naive ones).
    """
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _money(value) -> float:
    try:
        return round(float(str(value).replace(",", "").strip() or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


# ── Platform probe ───────────────────────────────────────────────────


def probe_platform(base: str, client: httpx.Client | None = None) -> str:
    """Is this Shopify? One request, and the answer is cached on the row.

    Asks for a single product rather than the whole catalogue: it's the same
    endpoint the collector will use, so a positive answer here means the
    collector will work, not merely that the site looks Shopify-shaped.
    """
    from dashboard.models import CompetitorPlatform

    own = client is None
    client = client or _client()
    try:
        resp = client.get(f"{base}/products.json", params={"limit": 1})
        if resp.status_code == 200 and "products" in (resp.json() or {}):
            return CompetitorPlatform.shopify.value
    except Exception as e:  # noqa: BLE001 - any failure means "not readable"
        log.info("platform probe for %s failed: %s", base, e)
    finally:
        if own:
            client.close()
    return CompetitorPlatform.other.value


# ── Shopify catalogue ────────────────────────────────────────────────


def _product_from_shopify(node: dict, base: str) -> RawProduct:
    variants = node.get("variants") or []
    prices = [_money(v.get("price")) for v in variants]
    prices = [p for p in prices if p > 0]
    handle = str(node.get("handle") or node.get("id") or "").strip()
    return RawProduct(
        handle=handle,
        external_id=str(node.get("id")) if node.get("id") is not None else None,
        title=(node.get("title") or handle or "Untitled").strip(),
        vendor=(node.get("vendor") or None),
        product_type=(node.get("product_type") or None),
        url=f"{base}/products/{handle}" if handle else None,
        price_min=min(prices) if prices else 0.0,
        price_max=max(prices) if prices else 0.0,
        currency=None,  # /products.json omits it; the storefront's own currency.
        available=any(v.get("available") for v in variants) if variants else None,
        published_at=_parse_dt(node.get("published_at")),
    )


def fetch_shopify_catalog(base: str, client: httpx.Client | None = None) -> Fetched:
    """Every product, paged. Stops at the first empty page."""
    own = client is None
    client = client or _client()
    out = Fetched()
    try:
        for page in range(1, _MAX_PAGES + 1):
            resp = client.get(
                f"{base}/products.json", params={"limit": 250, "page": page}
            )
            if resp.status_code != 200:
                if page == 1:
                    raise FetchError(
                        f"{base}/products.json returned HTTP {resp.status_code}."
                    )
                break
            nodes = (resp.json() or {}).get("products") or []
            out.pages = page
            if not nodes:
                break
            for node in nodes:
                product = _product_from_shopify(node, base)
                if product.handle:
                    out.products.append(product)
            if len(nodes) < 250:
                break
            time.sleep(_PAUSE)
    finally:
        if own:
            client.close()
    return out


_HANDLE_IN_HTML = re.compile(r"/products/([a-z0-9][a-z0-9\-_]{2,120})")


def _ordered_handles(html: str) -> list[str]:
    """Product handles in the order the page lists them, de-duplicated.

    A collection page links each product several times (image, title, quick
    view), so first-appearance order is the display order.
    """
    seen: list[str] = []
    for handle in _HANDLE_IN_HTML.findall(html):
        if handle not in seen:
            seen.append(handle)
    return seen


def fetch_shopify_bestsellers(
    base: str, limit: int = 250, client: httpx.Client | None = None
) -> list[str]:
    """Product handles in best-selling order, best first.

    `/collections/all?sort_by=best-selling` is a public storefront URL and the
    order Shopify renders *is* the store's own sales ranking — the closest
    thing to a competitor's sales data that exists publicly. Everything else
    on their site says what they stock, not what moves.

    This reads the **HTML** page, not `/collections/all/products.json`, and
    that is not an oversight: the JSON endpoint silently ignores `sort_by`.
    Confirmed against a live store — `best-selling`, `title-ascending` and
    no sort at all returned byte-identical results. Using it would have
    recorded alphabetical order as a sales ranking, which is worse than
    having no ranking at all because it looks like data.

    Returns handles only; the catalogue collector already has the details and
    this contributes nothing but the ordering.

    One page — typically the top 24-50 depending on their theme. Paging
    deeper would cost a request per page for ranks nobody acts on; the
    question this answers is "what are they pushing", and that lives at the
    top of the list.
    """
    own = client is None
    client = client or _client()
    try:
        resp = client.get(f"{base}/collections/all", params={"sort_by": "best-selling"})
        if resp.status_code != 200:
            raise FetchError(
                f"Best-seller collection returned HTTP {resp.status_code}."
            )
        ranked = _ordered_handles(resp.text)
        if not ranked:
            raise FetchError(
                "Best-seller page listed no products — the theme may not use "
                "standard /products/ links."
            )
        if ranked == sorted(ranked):
            # Alphabetical is what a page looks like when it ignored the sort
            # (some themes hard-code their own order, and the JSON endpoint
            # does it always). A real sales ranking being alphabetical by
            # accident is vanishingly unlikely past a handful of products.
            raise FetchError(
                "Best-seller ordering was ignored by this site — the results "
                "came back alphabetical, so no ranking was recorded."
            )
        return ranked[:limit]
    finally:
        if own:
            client.close()


# ── Blog posts ───────────────────────────────────────────────────────

_ENTRY = re.compile(r"<entry\b.*?</entry>", re.S | re.I)
_ITEM = re.compile(r"<item\b.*?</item>", re.S | re.I)


def _tag(block: str, name: str) -> str | None:
    m = re.search(rf"<{name}\b[^>]*>(.*?)</{name}>", block, re.S | re.I)
    if not m:
        return None
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", m.group(1), flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    # Feeds escape their content, and a title stored as "Laminate &amp; Vinyl"
    # renders as that literally — Jinja escapes on output, so an entity left
    # in here is displayed rather than decoded.
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _link(block: str) -> str | None:
    m = re.search(r'<link\b[^>]*href="([^"]+)"', block, re.I)
    if m:
        return m.group(1)
    return _tag(block, "link")


def parse_feed(xml: str) -> list[RawPost]:
    """Atom or RSS, whichever this is.

    Regex rather than an XML parser on purpose: these feeds come from other
    people's servers, they are frequently slightly malformed, and a strict
    parser turns "one stray ampersand" into "this competitor has no blog".
    Nothing here is executed or trusted — it's read, truncated and displayed.
    """
    blocks = _ENTRY.findall(xml) or _ITEM.findall(xml)
    posts: list[RawPost] = []
    for block in blocks:
        url = _link(block)
        title = _tag(block, "title")
        if not url or not title:
            continue
        summary = _tag(block, "summary") or _tag(block, "description")
        published = (
            _tag(block, "published")
            or _tag(block, "updated")
            or _tag(block, "pubDate")
        )
        posts.append(
            RawPost(
                url=url,
                title=title[:600],
                summary=(summary or "")[:1000] or None,
                author=_tag(block, "name") or _tag(block, "author"),
                published_at=_parse_dt(published) or _parse_rfc822(published),
            )
        )
    return posts


def _parse_rfc822(value: str | None) -> datetime | None:
    """RSS dates are RFC 822 (`Tue, 05 Aug 2026 10:00:00 GMT`), which
    fromisoformat won't touch."""
    if not value:
        return None
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed and parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


#: Where blogs live, most likely first. Shopify's default is /blogs/news.
_FEED_PATHS = (
    "/blogs/news.atom",
    "/blogs/blog.atom",
    "/feed",
    "/rss.xml",
    "/feed.xml",
    "/blog/feed",
    "/blog/rss.xml",
)


def fetch_posts(base: str, client: httpx.Client | None = None) -> Fetched:
    """The blog feed, from whichever of the usual paths answers first."""
    own = client is None
    client = client or _client()
    out = Fetched()
    try:
        for path in _FEED_PATHS:
            try:
                resp = client.get(base + path)
            except Exception:  # noqa: BLE001 - try the next candidate
                continue
            out.pages += 1
            if resp.status_code != 200 or not resp.text.strip().startswith("<"):
                time.sleep(_PAUSE)
                continue
            posts = parse_feed(resp.text)
            if posts:
                out.posts = posts
                return out
            time.sleep(_PAUSE)
        raise FetchError(
            "No blog feed found. Tried: " + ", ".join(_FEED_PATHS) + "."
        )
    finally:
        if own:
            client.close()


# ── JSON-LD, for sites that aren't Shopify ───────────────────────────

_LD_BLOCK = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)


def extract_jsonld_products(html: str, url: str) -> list[RawProduct]:
    """`schema.org/Product` nodes out of one page's JSON-LD.

    Sites emit this for Google, so it's present far more often than not, and
    it carries exactly what's wanted: name, brand, and an `offers` price.
    """
    out: list[RawProduct] = []
    for block in _LD_BLOCK.findall(html):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for node in _iter_ld_nodes(data):
            if str(node.get("@type", "")).lower() != "product":
                continue
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            prices = [
                _money(offers.get(k))
                for k in ("price", "lowPrice", "highPrice")
                if offers.get(k) is not None
            ]
            prices = [p for p in prices if p > 0]
            brand = node.get("brand")
            if isinstance(brand, dict):
                brand = brand.get("name")
            name = str(node.get("name") or "").strip()
            if not name:
                continue
            out.append(
                RawProduct(
                    handle=url.rstrip("/").rsplit("/", 1)[-1] or name.lower(),
                    title=name[:600],
                    vendor=str(brand)[:200] if brand else None,
                    url=str(node.get("url") or url),
                    price_min=min(prices) if prices else 0.0,
                    price_max=max(prices) if prices else 0.0,
                    currency=offers.get("priceCurrency"),
                )
            )
    return out


def _iter_ld_nodes(data):
    """JSON-LD is a graph, a list, or a bare object depending on the site."""
    if isinstance(data, list):
        for item in data:
            yield from _iter_ld_nodes(item)
    elif isinstance(data, dict):
        if "@graph" in data:
            yield from _iter_ld_nodes(data["@graph"])
        else:
            yield data
