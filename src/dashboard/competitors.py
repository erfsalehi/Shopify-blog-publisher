"""Reading a competitor's public site.

Everything here fetches pages any browser can fetch, at a browsing pace, and
parses what the site publishes for machines. No login, no paid API, no
attempt to get at anything a site hasn't chosen to expose.

The order of preference is the whole design:

  1. **Shopify's own JSON.** Most local flooring retailers run Shopify, and
     Shopify publishes the entire catalogue at `/products.json` and the blog
     as Atom. That's a documented, stable, paginated feed — no HTML parsing,
     nothing that breaks when they change themes.
  2. **Sitemap + JSON-LD.** Sites that aren't Shopify usually still emit
     `schema.org/Product` for Google's benefit. Same data, one request per
     product instead of one per 250 — so it's capped per run and the
     rotation picks up where it left off.
  3. **OpenGraph** (`product:price:amount`), which storefronts emit for
     Facebook when they emit nothing else.
  4. **A CSS selector the owner sets**, for the site that publishes neither.
     Two minutes with a browser inspector beats a scraping arms race.
  5. **Nothing.** Recorded on the competitor with the reason, and reported
     on the job, rather than retried nightly as though it might start
     working.

Each step is tried only when the one before it found no price, so a site
with good markup never reaches the fragile path — and `price_sources` on the
result says which step actually paid, so a silent slide down to step 4 is
visible before it breaks.

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
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)

#: Seconds between requests to the same host. A human clicking through a
#: catalogue is slower than this.
_PAUSE = 1.0
#: Pages of 250 products per run. 40 was chosen on the assumption that no
#: local flooring retailer has 10,000 products; one of them has more, and
#: that run took 57.5s against a 60s function ceiling — one slow second from
#: being killed mid-write. 20 pages (5,000 products) lands around 25s, and
#: `next_page` makes the rest a resumption rather than a blind spot.
_MAX_PAGES = 20
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
    #: Which extraction step produced each price, for the non-Shopify path.
    #: Reported on the job so a site quietly falling back to the fragile
    #: selector route is visible before it breaks, rather than after.
    price_sources: dict = field(default_factory=dict)
    #: Where the next run should resume, or None when the feed ended. None
    #: means "this was a complete pass" — the difference between a product
    #: count that is their catalogue and one that is only what fit in 60
    #: seconds.
    next_page: int | None = None


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


# ── robots.txt ───────────────────────────────────────────────────────

#: One parser per host, for the life of the process. A serverless invocation
#: reads one competitor, so this is fetched once per run rather than once per
#: request — which is the point: checking politeness shouldn't itself be the
#: impolite part.
_ROBOTS: dict[str, RobotFileParser | None] = {}

#: The name a site would write in its robots.txt to address this
#: specifically. Matched by urllib's parser as a prefix of the full UA above.
ROBOTS_AGENT = "DRFlooringControlCenter"


def _robots_for(base: str, client: httpx.Client | None = None) -> RobotFileParser | None:
    """The site's robots.txt, parsed. None when it has none, or it's
    unreadable — both of which mean "no stated restrictions".

    Deliberately fetched with httpx rather than `RobotFileParser.read()`:
    that method uses urllib, which ignores this module's timeout, its
    User-Agent, and this machine's proxy behaviour. A robots fetch that hangs
    for 60s would take the whole job with it.
    """
    host = base.rstrip("/")
    if host in _ROBOTS:
        return _ROBOTS[host]

    own = client is None
    client = client or _client()
    parser: RobotFileParser | None = None
    try:
        resp = client.get(f"{host}/robots.txt")
        if resp.status_code == 200 and resp.text.strip():
            parser = RobotFileParser()
            parser.parse(resp.text.splitlines())
        # 4xx means no robots.txt, which permits everything. A 5xx arguably
        # means "unknown", but treating a competitor's flaky server as a
        # blanket ban would silently stop collecting with no explanation.
    except Exception as e:  # noqa: BLE001 - unreadable robots is not a block
        log.info("robots.txt for %s unreadable (%s); proceeding", host, e)
    finally:
        if own:
            client.close()

    _ROBOTS[host] = parser
    return parser


def may_fetch(base: str, path: str, client: httpx.Client | None = None) -> bool:
    """Whether robots.txt permits fetching `path` on this host.

    PLAN.md asks for this explicitly, and it matters more here than the usual
    hand-wave: these are small local businesses' sites, and the difference
    between reading a public catalogue and ignoring a site's stated wishes is
    exactly this check.
    """
    parser = _robots_for(base, client=client)
    if parser is None:
        return True
    url = path if path.startswith("http") else base.rstrip("/") + path
    return parser.can_fetch(ROBOTS_AGENT, url)


def _require_allowed(base: str, path: str, client: httpx.Client | None = None) -> None:
    if not may_fetch(base, path, client=client):
        raise FetchError(
            f"robots.txt on this site disallows {path}. Not fetched — that's "
            "the site's decision to make."
        )


def reset_robots_cache() -> None:
    """For tests, and for a long-lived local process that shouldn't hold a
    site's robots.txt from last week."""
    _ROBOTS.clear()


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


def fetch_shopify_catalog(
    base: str,
    client: httpx.Client | None = None,
    *,
    start_page: int = 1,
    max_pages: int = _MAX_PAGES,
) -> Fetched:
    """A slice of the catalogue, starting at `start_page`.

    Sets `next_page` to where the following run should resume, or None when
    the feed ended — which is what tells the caller a full pass is done and
    the product count can finally be trusted as their catalogue size.

    Sliced rather than exhaustive because a 60-second function can't page an
    unbounded catalogue: one real competitor has 10,000+ products and took
    57.5s to reach the old cap. Being killed mid-write would lose the whole
    run, since it's one transaction.
    """
    own = client is None
    client = client or _client()
    out = Fetched()
    start_page = max(1, start_page)
    try:
        _require_allowed(base, "/products.json", client=client)
        page = start_page
        for offset in range(max_pages):
            page = start_page + offset
            resp = client.get(
                f"{base}/products.json", params={"limit": 250, "page": page}
            )
            if resp.status_code != 200:
                if offset == 0:
                    raise FetchError(
                        f"{base}/products.json returned HTTP {resp.status_code}."
                    )
                out.next_page = None  # treat as the end rather than a hole
                return out
            nodes = (resp.json() or {}).get("products") or []
            out.pages += 1
            if not nodes:
                out.next_page = None
                return out
            for node in nodes:
                product = _product_from_shopify(node, base)
                if product.handle:
                    out.products.append(product)
            if len(nodes) < 250:
                out.next_page = None  # short page means the last page
                return out
            time.sleep(_PAUSE)
        # Ran out of budget, not out of catalogue.
        out.next_page = page + 1
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
        _require_allowed(base, "/collections/all", client=client)
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
            # Per-path rather than one check up front: these are candidate
            # guesses, and a site disallowing /feed shouldn't stop us trying
            # the one it does publish.
            if not may_fetch(base, path, client=client):
                continue
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


# ── Sites that aren't Shopify ────────────────────────────────────────
#
# Everything above this line reads one documented feed. Below it is the
# fallback: discover product URLs from the sitemap, fetch each page, and pull
# a price out of whatever the page publishes for machines. That's a request
# per product instead of 250, so it is deliberately capped and deliberately
# slow, and it is only reached when /products.json isn't there.

#: Product pages fetched per run for a non-Shopify site. At `_PAUSE` a second
#: that's ~40s of wall clock, which fits a 60s function with room to write.
#: The rotation means the next run continues where this one stopped.
_MAX_PRODUCT_PAGES = 40

_SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml")
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
#: Sitemaps that are themselves lists of sitemaps.
_SITEMAP_LIKE = re.compile(r"sitemap.*\.xml", re.I)
#: What a product URL tends to look like across Woo, BigCommerce, Magento and
#: hand-rolled sites. Not exhaustive — it doesn't need to be, because a miss
#: costs one skipped product and a false positive costs one wasted fetch that
#: yields no price and is dropped.
_PRODUCT_URL = re.compile(r"/(product|products|shop|item|p)/", re.I)

_OG_PRICE = re.compile(
    r'<meta[^>]+property=["\'](?:product:price:amount|og:price:amount)["\']'
    r'[^>]+content=["\']([^"\']+)["\']',
    re.I,
)
_OG_PRICE_REVERSED = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+'
    r'property=["\'](?:product:price:amount|og:price:amount)["\']',
    re.I,
)
_OG_CURRENCY = re.compile(
    r'<meta[^>]+property=["\'](?:product:price:currency|og:price:currency)["\']'
    r'[^>]+content=["\']([^"\']+)["\']',
    re.I,
)
_OG_TITLE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)["\']', re.I
)
_TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)

#: A price inside whatever element the owner's CSS selector points at. Only
#: the class/id form is supported — see `price_from_selector`.
_MONEY_IN_TEXT = re.compile(r"[$£€]?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")


def discover_product_urls(
    base: str, limit: int = _MAX_PRODUCT_PAGES, client: httpx.Client | None = None
) -> list[str]:
    """Product page URLs from the site's sitemap.

    Sitemaps are how a site tells crawlers what it wants found, which makes
    them the right door for this and not merely the convenient one. One level
    of sitemap-index nesting is followed; deeper than that is rare and the
    cap would truncate it anyway.
    """
    own = client is None
    client = client or _client()
    found: list[str] = []
    try:
        roots: list[str] = []
        for path in _SITEMAP_PATHS:
            if not may_fetch(base, path, client=client):
                continue
            try:
                resp = client.get(base + path)
            except Exception:  # noqa: BLE001 - try the next candidate
                continue
            if resp.status_code == 200 and "<" in resp.text:
                roots.append(resp.text)
                break
            time.sleep(_PAUSE)
        if not roots:
            raise FetchError(
                "No sitemap found. Tried: " + ", ".join(_SITEMAP_PATHS) + "."
            )

        locs = _LOC.findall(roots[0])
        nested = [u for u in locs if _SITEMAP_LIKE.search(u)][:10]
        direct = [u for u in locs if not _SITEMAP_LIKE.search(u)]

        for url in nested:
            if len(found) >= limit:
                break
            if not may_fetch(base, url, client=client):
                continue
            try:
                resp = client.get(url)
            except Exception:  # noqa: BLE001
                continue
            time.sleep(_PAUSE)
            if resp.status_code == 200:
                direct.extend(_LOC.findall(resp.text))

        seen: set[str] = set()
        for url in direct:
            if len(found) >= limit:
                break
            if url in seen or not _PRODUCT_URL.search(url):
                continue
            seen.add(url)
            found.append(url)
        if not found:
            raise FetchError(
                "The sitemap listed no URLs that look like product pages "
                "(no /product/, /shop/ or /item/ path)."
            )
    finally:
        if own:
            client.close()
    return found


def price_from_opengraph(html: str) -> tuple[float, str | None]:
    """`product:price:amount`, which most storefronts emit for Facebook.

    Two patterns because attribute order isn't fixed and a single regex that
    allowed either order would also match a `content` from one tag paired
    with a `property` from the next.
    """
    match = _OG_PRICE.search(html) or _OG_PRICE_REVERSED.search(html)
    if not match:
        return 0.0, None
    currency = _OG_CURRENCY.search(html)
    return _money(match.group(1)), (currency.group(1) if currency else None)


def price_from_selector(html: str, selector: str) -> float:
    """A price from the element the owner pointed at.

    Supports `.class` and `#id`, which is what a price element realistically
    is, and is what someone can read off their browser's inspector in the two
    minutes PLAN.md budgets for this. Deliberately not a CSS engine: pulling
    in a parser to support `div > span:nth-child(2)` would be a dependency
    and a maintenance burden for a selector nobody can write reliably from
    memory anyway.

    Returns 0.0 when the selector matches nothing, which the caller treats as
    "no price" rather than as free.
    """
    selector = (selector or "").strip()
    if not selector:
        return 0.0
    kind, name = selector[0], re.escape(selector[1:])
    if kind == ".":
        attr = rf'class=["\'][^"\']*\b{name}\b[^"\']*["\']'
    elif kind == "#":
        attr = rf'id=["\']{name}["\']'
    else:
        return 0.0

    # The element's own text, up to its closing tag. Non-greedy so a matching
    # <span> doesn't swallow the rest of the page.
    element = re.search(rf"<([a-z0-9]+)[^>]*{attr}[^>]*>(.*?)</\1>", html, re.S | re.I)
    if not element:
        return 0.0
    text = unescape(re.sub(r"<[^>]+>", " ", element.group(2)))
    money = _MONEY_IN_TEXT.search(text)
    return _money(money.group(1)) if money else 0.0


def _title_from(html: str, url: str) -> str:
    og = _OG_TITLE.search(html)
    if og and og.group(1).strip():
        return unescape(og.group(1)).strip()[:600]
    tag = _TITLE_TAG.search(html)
    if tag:
        text = unescape(re.sub(r"<[^>]+>", " ", tag.group(1)))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            return text[:600]
    return url.rstrip("/").rsplit("/", 1)[-1][:600] or url[:600]


def fetch_generic_catalog(
    base: str,
    *,
    price_selector: str | None = None,
    limit: int = _MAX_PRODUCT_PAGES,
    client: httpx.Client | None = None,
) -> Fetched:
    """Products from a non-Shopify site, one page at a time.

    Extraction order is PLAN.md's, and the order matters: JSON-LD is
    structured and unambiguous, OpenGraph is structured but flatter, and the
    owner's CSS selector is a last resort that only exists because some sites
    publish neither. Each is tried only if the one before it found nothing,
    so a site with good markup never reaches the fragile path.
    """
    own = client is None
    client = client or _client()
    out = Fetched()
    sources = {"json-ld": 0, "opengraph": 0, "selector": 0, "none": 0}
    try:
        urls = discover_product_urls(base, limit=limit, client=client)
        for url in urls:
            if not may_fetch(base, url, client=client):
                continue
            try:
                resp = client.get(url)
            except Exception as e:  # noqa: BLE001 - one bad page, not the run
                log.info("competitor page %s failed: %s", url, e)
                continue
            out.pages += 1
            time.sleep(_PAUSE)
            if resp.status_code != 200:
                continue
            html = resp.text

            products = extract_jsonld_products(html, url)
            product = products[0] if products else None
            if product and product.price_min > 0:
                sources["json-ld"] += 1
            else:
                price, currency = price_from_opengraph(html)
                if price > 0:
                    sources["opengraph"] += 1
                elif price_selector:
                    price = price_from_selector(html, price_selector)
                    if price > 0:
                        sources["selector"] += 1
                if product is None:
                    product = RawProduct(
                        handle=url.rstrip("/").rsplit("/", 1)[-1] or url,
                        title=_title_from(html, url),
                        url=url,
                    )
                if price > 0:
                    product.price_min = product.price_max = price
                    product.currency = product.currency or currency
            if product is None:
                continue
            if product.price_min <= 0:
                sources["none"] += 1
            product.url = product.url or url
            out.products.append(product)

        if not out.products:
            raise FetchError(
                f"Fetched {out.pages} product pages and could not read a "
                "product from any of them. If this site shows prices, set a "
                "price CSS selector for it on the Settings page."
            )
    finally:
        if own:
            client.close()
    out.price_sources = sources
    return out
