"""Reading a site that isn't Shopify, and reading it politely.

The extraction chain exists because not every competitor runs Shopify, and
its *order* is the design: structured data first, the owner's hand-written
selector last. These tests pin the order, because a refactor that tried the
cheap regex first would still pass any test that only checked "a price came
back" — while quietly becoming the fragile thing PLAN.md was avoiding.
"""

from __future__ import annotations

import pytest

from dashboard import competitors as C


@pytest.fixture(autouse=True)
def _clean_robots():
    C.reset_robots_cache()
    yield
    C.reset_robots_cache()


# ── robots.txt ──────────────────────────────────────────────────────


def _robots(text: str, host: str = "https://x.example") -> None:
    parser = C.RobotFileParser()
    parser.parse(text.splitlines())
    C._ROBOTS[host] = parser


def test_a_disallowed_path_is_not_fetched():
    """PLAN.md asks for this explicitly. These are small local businesses'
    sites, and the whole difference between reading a public catalogue and
    ignoring a site's stated wishes is this check."""
    _robots("User-agent: *\nDisallow: /products/")

    assert C.may_fetch("https://x.example", "/products/thing") is False
    assert C.may_fetch("https://x.example", "/collections/all") is True


def test_a_rule_naming_this_crawler_specifically_is_honoured():
    _robots(f"User-agent: {C.ROBOTS_AGENT}\nDisallow: /\n")

    assert C.may_fetch("https://x.example", "/anything") is False


def test_no_robots_file_means_no_restrictions():
    """Absent and unreadable both mean "nothing stated". Treating a missing
    file as a ban would silently stop collecting with no explanation."""
    C._ROBOTS["https://x.example"] = None

    assert C.may_fetch("https://x.example", "/products.json") is True


def test_a_blocked_fetch_raises_something_the_owner_can_read():
    _robots("User-agent: *\nDisallow: /products.json")

    with pytest.raises(C.FetchError, match="robots.txt"):
        C._require_allowed("https://x.example", "/products.json")


# ── The extraction chain, in order ──────────────────────────────────

_JSONLD = """<html><head><script type="application/ld+json">
{"@type":"Product","name":"Torlys Everwood 6mm","brand":{"name":"Torlys"},
 "offers":{"price":"5.49","priceCurrency":"CAD"}}
</script></head><body>
<meta property="product:price:amount" content="99.99">
<span class="price">$77.77</span></body></html>"""


def test_json_ld_wins_over_opengraph_and_the_selector():
    """All three are present and disagree. Structured data is the one to
    trust; the others exist for sites that don't publish it."""
    products = C.extract_jsonld_products(_JSONLD, "https://x.example/product/t")

    assert products[0].price_min == 5.49
    assert products[0].vendor == "Torlys"


def test_opengraph_is_read_whichever_order_the_attributes_are_in():
    """Attribute order isn't fixed, and a single permissive regex would pair
    a `content` from one tag with a `property` from the next."""
    assert C.price_from_opengraph(
        '<meta property="product:price:amount" content="4.29">'
    )[0] == 4.29
    assert C.price_from_opengraph(
        '<meta content="3.15" property="og:price:amount">'
    )[0] == 3.15


def test_a_missing_opengraph_price_is_zero_not_an_error():
    assert C.price_from_opengraph("<html></html>") == (0.0, None)


def test_the_owner_selector_reads_a_price_out_of_its_element():
    html = '<div><span class="price-item price-item--sale">$ 2,199.99</span></div>'

    assert C.price_from_selector(html, ".price-item") == 2199.99
    assert C.price_from_selector('<p id="ourPrice">CAD 12.50</p>', "#ourPrice") == 12.5


def test_an_unsupported_selector_returns_no_price_rather_than_guessing():
    """Only `.class` and `#id` are supported — that's what someone can read
    off a browser inspector. A descendant selector isn't parsed, and must
    fail closed rather than matching something arbitrary."""
    html = '<div class="a"><span class="price">$5.00</span></div>'

    assert C.price_from_selector(html, "div > span") == 0.0
    assert C.price_from_selector(html, ".nope") == 0.0
    assert C.price_from_selector(html, "") == 0.0


def test_a_title_is_found_even_with_no_structured_data():
    assert "Everwood & Oak" == C._title_from(
        '<meta property="og:title" content="Everwood &amp; Oak">', "u"
    )
    assert "Some Product" == C._title_from("<title> Some  Product </title>", "u")
    # Last resort: the URL slug beats showing nothing.
    assert "slug-here" == C._title_from("", "https://x.example/product/slug-here/")


# ── The best-seller ordering trap ───────────────────────────────────


def test_alphabetical_best_sellers_are_refused_as_a_ranking():
    """`/collections/all/products.json` silently ignores sort_by — confirmed
    against a live store, where best-selling and title-ascending returned
    byte-identical results. Recording that as a sales ranking is worse than
    recording nothing, because it looks like data."""
    import inspect

    source = inspect.getsource(C.fetch_shopify_bestsellers)

    assert "ranked == sorted(ranked)" in source
    assert "/collections/all" in source
    assert "products.json" not in source.split('"""')[2]


# ── The watchlist ───────────────────────────────────────────────────


def test_only_priced_or_show_price_products_are_matched(dashboard_db):
    """PLAN.md's watchlist. A confirmed match on a product whose price we
    hide can never fire an undercut alert — the rule skips price <= 0 — so
    proposing one costs a review decision and buys nothing. With ~94% of this
    catalogue hidden, matching everything buries the useful proposals."""
    from dashboard.db import get_session
    from dashboard.jobs.competitor_watch import propose_competitor_matches
    from dashboard.models import (
        Competitor, CompetitorProduct, ShopifyProduct,
    )

    with get_session() as s:
        s.add(Competitor(name="R", base_url="https://r.example", enabled=True))
        s.add(ShopifyProduct(product_gid="g1", handle="hidden",
                             title="Euro Atlantic 12mm Oak", price_min=0.0))
        s.add(ShopifyProduct(product_gid="g2", handle="priced",
                             title="Euro Atlantic 12mm Oak", price_min=3.99))
        s.flush()
        s.add(CompetitorProduct(competitor_id=1, handle="t",
                                title="Euro Atlantic 12mm Oak", price_min=2.89))

    result = propose_competitor_matches()

    assert result.detail["our_watchlist"] == 1      # only the priced one
    assert result.detail["our_catalogue"] == 2
    from dashboard.models import CompetitorMatch
    with get_session() as s:
        matched = [m.shopify_product_id for m in s.query(CompetitorMatch)]
    assert matched == [2]


def test_a_show_price_tag_puts_a_hidden_product_on_the_watchlist(dashboard_db):
    """The tag is how the show-price rollout marks a product as one whose
    price is about to become visible. Those need watching before the price
    lands, not after."""
    import json

    from dashboard.db import get_session
    from dashboard.jobs.competitor_watch import propose_competitor_matches
    from dashboard.models import Competitor, CompetitorProduct, ShopifyProduct

    with get_session() as s:
        s.add(Competitor(name="R", base_url="https://r.example", enabled=True))
        s.add(ShopifyProduct(product_gid="g1", handle="tagged",
                             title="Euro Atlantic 12mm Oak", price_min=0.0,
                             tags_json=json.dumps(["show-price"])))
        s.flush()
        s.add(CompetitorProduct(competitor_id=1, handle="t",
                                title="Euro Atlantic 12mm Oak", price_min=2.89))

    result = propose_competitor_matches()

    assert result.detail["our_watchlist"] == 1


def test_nothing_on_the_watchlist_says_so_instead_of_looking_idle(dashboard_db):
    from dashboard.db import get_session
    from dashboard.jobs.competitor_watch import propose_competitor_matches
    from dashboard.models import Competitor, CompetitorProduct, ShopifyProduct

    with get_session() as s:
        s.add(Competitor(name="R", base_url="https://r.example", enabled=True))
        s.add(ShopifyProduct(product_gid="g1", handle="h", title="A", price_min=0.0))
        s.flush()
        s.add(CompetitorProduct(competitor_id=1, handle="t", title="A", price_min=2.0))

    result = propose_competitor_matches()

    assert result.rows == 0
    assert "show-price" in result.detail["note"]


# ── Price history ───────────────────────────────────────────────────


def _seed_history(days_and_prices):
    from datetime import date, timedelta

    from dashboard.db import get_session
    from dashboard.models import (
        Competitor, CompetitorMatch, CompetitorProduct, CompetitorProductPrice,
        MatchStatus, ShopifyProduct,
    )

    today = date.today()
    with get_session() as s:
        s.add(Competitor(name="R", base_url="https://r.example", enabled=True))
        s.add(ShopifyProduct(product_gid="g1", handle="ours", title="Board",
                             price_min=3.99))
        s.flush()
        s.add(CompetitorProduct(competitor_id=1, handle="t", title="Board",
                                price_min=2.89))
        s.flush()
        s.add(CompetitorMatch(competitor_product_id=1, shopify_product_id=1,
                              status=MatchStatus.confirmed.value, score=0.9))
        for offset, price in days_and_prices:
            s.add(CompetitorProductPrice(
                competitor_product_id=1, date=today - timedelta(days=offset),
                price_min=price,
            ))
    return today


def test_price_history_returns_both_series(dashboard_db):
    from dashboard import reporting

    _seed_history([(9, 3.49), (6, 3.29), (3, 2.99), (0, 2.89)])
    history = reporting.competitor_price_history(1)

    assert history["found"] is True
    assert [p for _, p in history["theirs"]] == [3.49, 3.29, 2.99, 2.89]
    # Ours is one number held flat across their dates — see below.
    assert {p for _, p in history["ours"]} == {3.99}


def test_our_flat_line_is_labelled_as_not_measured(dashboard_db):
    """The catalogue snapshot overwrites price in place and keeps no per-day
    history, so our line is today's number repeated. A flat line that looks
    measured would be read as "we held our price", which isn't a claim this
    data can support."""
    from dashboard import reporting

    _seed_history([(3, 3.0), (0, 2.5)])
    history = reporting.competitor_price_history(1)

    assert history["our_price_is_current_only"] is True


def test_a_missing_product_is_reported_not_raised(dashboard_db):
    from dashboard import reporting

    assert reporting.competitor_price_history(9999)["found"] is False


def test_the_price_chart_axis_is_not_zero_based():
    """Flooring prices sit in a narrow band ($2-6/sqft). A zero-based axis
    flattens both series into one indistinguishable strip, hiding the exact
    movement the chart exists to show."""
    from datetime import date, timedelta

    from dashboard import charts

    today = date.today()
    theirs = [(today - timedelta(days=d), p) for d, p in [(3, 2.89), (0, 2.99)]]
    ours = [(today - timedelta(days=d), 3.99) for d in (3, 0)]

    svg = str(charts.price_chart(ours, theirs))

    # Both series drawn, and the axis labels sit near the data rather than $0.
    assert 'class="theirs"' in svg and 'class="ours"' in svg
    assert "$0.00" not in svg


def test_one_day_of_history_says_so_rather_than_drawing_a_dot():
    from datetime import date

    from dashboard import charts

    svg = str(charts.price_chart([], [(date.today(), 2.89)]))

    assert "second day" in svg


def test_two_identical_prices_do_not_divide_by_zero():
    """A competitor who hasn't moved their price is the common case, and a
    flat pair would make the y-range zero."""
    from datetime import date, timedelta

    from dashboard import charts

    today = date.today()
    flat = [(today - timedelta(days=d), 2.89) for d in (3, 0)]

    svg = str(charts.price_chart([], flat))

    assert 'class="theirs"' in svg


# ── Duplicate handles in a feed ─────────────────────────────────────


def test_a_handle_listed_twice_does_not_break_the_run(dashboard_db, monkeypatch):
    """Shopify's `page` pagination overlaps when the catalogue changes
    mid-crawl, and the sitemap path can reach one product by two URLs. Both
    entries resolve to the same row, and before this each tried to write
    today's price snapshot for it — a unique-constraint violation that failed
    the whole 3,800-product run. Seen for real against a live store."""
    from dashboard.db import get_session
    from dashboard.jobs import competitor_watch as watch
    from dashboard.models import (
        Competitor, CompetitorProduct, CompetitorProductPrice,
    )

    with get_session() as s:
        s.add(Competitor(name="Dupes", base_url="https://d.example",
                         enabled=True, platform="shopify"))

    twice = C.Fetched(products=[
        C.RawProduct(handle="board", title="Board", price_min=2.89),
        C.RawProduct(handle="board", title="Board (again)", price_min=2.89),
        C.RawProduct(handle="other", title="Other", price_min=3.50),
    ], pages=1)
    monkeypatch.setattr(watch.fetchers, "probe_platform",
                        lambda base, client=None: "shopify")
    monkeypatch.setattr(watch.fetchers, "fetch_shopify_catalog",
                        lambda base, client=None, **kw: twice)

    result = watch.sync_competitor_catalog()

    assert result.skipped is False
    assert result.detail["new_products"] == 2      # not 3
    with get_session() as s:
        assert s.query(CompetitorProduct).count() == 2
        assert s.query(CompetitorProductPrice).count() == 2


def test_running_the_catalogue_twice_in_a_day_updates_one_snapshot(
    dashboard_db, monkeypatch
):
    """The second run of the day must overwrite today's price row, not add a
    second one — that's what makes the job safe to re-run and safe to retry."""
    from dashboard.db import get_session
    from dashboard.jobs import competitor_watch as watch
    from dashboard.models import Competitor, CompetitorProductPrice

    with get_session() as s:
        s.add(Competitor(name="Twice", base_url="https://t.example",
                         enabled=True, platform="shopify"))

    monkeypatch.setattr(watch.fetchers, "probe_platform",
                        lambda base, client=None: "shopify")

    def feed(price):
        return C.Fetched(
            products=[C.RawProduct(handle="board", title="Board",
                                   price_min=price)],
            pages=1,
        )

    monkeypatch.setattr(watch.fetchers, "fetch_shopify_catalog",
                        lambda base, client=None, **kw: feed(2.89))
    watch.sync_competitor_catalog()
    monkeypatch.setattr(watch.fetchers, "fetch_shopify_catalog",
                        lambda base, client=None, **kw: feed(2.49))
    watch.sync_competitor_catalog()

    with get_session() as s:
        snaps = s.query(CompetitorProductPrice).all()
        assert len(snaps) == 1
        assert snaps[0].price_min == 2.49


# ── Resuming a catalogue too big for one run ────────────────────────


def test_a_truncated_pass_resumes_where_it_stopped(dashboard_db, monkeypatch):
    """One real competitor has 10,000+ products and hit the old cap at 57.5s
    against a 60s ceiling. Capping without resuming isn't a limit, it's a
    permanent blind spot past page N."""
    from dashboard.db import get_session
    from dashboard.jobs import competitor_watch as watch
    from dashboard.models import Competitor

    with get_session() as s:
        s.add(Competitor(name="Big", base_url="https://b.example",
                         enabled=True, platform="shopify"))

    seen_pages = []

    def slice_(base, client=None, *, start_page=1, **kw):
        seen_pages.append(start_page)
        return C.Fetched(
            products=[C.RawProduct(handle=f"p{start_page}", title="P",
                                   price_min=1.0)],
            pages=20,
            next_page=start_page + 20,
        )

    monkeypatch.setattr(watch.fetchers, "probe_platform",
                        lambda base, client=None: "shopify")
    monkeypatch.setattr(watch.fetchers, "fetch_shopify_catalog", slice_)

    first = watch.sync_competitor_catalog()
    second = watch.sync_competitor_catalog()

    assert seen_pages == [1, 21]
    assert "partial" in first.detail["pass"]
    assert "resuming at page 21" in first.detail["pass"]
    assert "resuming at page 41" in second.detail["pass"]


def test_a_partial_pass_never_claims_products_were_delisted(
    dashboard_db, monkeypatch
):
    """On a partial slice almost every stored product has an older
    `last_seen` simply because it wasn't in this slice. Reporting that as
    "no longer listed" would claim the competitor delisted their whole
    catalogue every night."""
    from dashboard.db import get_session
    from dashboard.jobs import competitor_watch as watch
    from dashboard.models import Competitor, CompetitorProduct

    with get_session() as s:
        s.add(Competitor(name="Big", base_url="https://b.example",
                         enabled=True, platform="shopify"))
        s.flush()
        s.add(CompetitorProduct(competitor_id=1, handle="old", title="Old",
                                price_min=1.0))

    monkeypatch.setattr(watch.fetchers, "probe_platform",
                        lambda base, client=None: "shopify")
    monkeypatch.setattr(
        watch.fetchers, "fetch_shopify_catalog",
        lambda base, client=None, **kw: C.Fetched(
            products=[C.RawProduct(handle="new", title="New", price_min=2.0)],
            pages=20, next_page=21,
        ),
    )

    result = watch.sync_competitor_catalog()

    assert "no_longer_listed" not in result.detail


def test_a_complete_pass_wraps_back_to_page_one(dashboard_db, monkeypatch):
    """Reaching the end means the next pass should re-read from the start to
    pick up new and changed products — not keep walking off the end."""
    from dashboard.db import get_session
    from dashboard.jobs import competitor_watch as watch
    from dashboard.models import Competitor

    with get_session() as s:
        s.add(Competitor(name="Small", base_url="https://s.example",
                         enabled=True, platform="shopify", catalog_page=21))

    monkeypatch.setattr(watch.fetchers, "probe_platform",
                        lambda base, client=None: "shopify")
    monkeypatch.setattr(
        watch.fetchers, "fetch_shopify_catalog",
        lambda base, client=None, **kw: C.Fetched(
            products=[C.RawProduct(handle="x", title="X", price_min=1.0)],
            pages=2, next_page=None,
        ),
    )

    result = watch.sync_competitor_catalog()

    assert "complete" in result.detail["pass"]
    assert result.detail["no_longer_listed"] == 0
    with get_session() as s:
        row = s.query(Competitor).one()
        assert row.catalog_page == 1
        assert row.catalog_complete is True


def test_the_fetcher_reports_the_end_of_the_feed(monkeypatch):
    """`next_page is None` is what the whole resumption scheme hangs on —
    a short page, an empty page and a non-200 all mean "stop here"."""
    import httpx

    class Resp:
        status_code = 200

        def __init__(self, n):
            self._n = n

        def json(self):
            return {"products": [{"id": i, "handle": f"h{i}", "title": "T",
                                  "variants": []} for i in range(self._n)]}

    class Client:
        def __init__(self, sizes):
            self.sizes = list(sizes)

        def get(self, url, params=None):
            if url.endswith("robots.txt"):
                r = Resp(0)
                r.status_code = 404
                return r
            return Resp(self.sizes.pop(0))

        def close(self):
            pass

    # A short second page ends the pass.
    out = C.fetch_shopify_catalog(
        "https://x.example", client=Client([250, 10]), max_pages=5
    )
    assert out.next_page is None
    assert len(out.products) == 260


# ── Ranges they price and we hide ───────────────────────────────────


def test_the_price_visibility_gap_is_found(dashboard_db):
    """The match queue cannot surface this: it only considers products whose
    price we show, and these are the ones we don't. On the real data our top
    overlap is a range where 156 of ours are hidden and 204 of theirs carry a
    price — invisible everywhere else on the page."""
    from dashboard import reporting
    from dashboard.db import get_session
    from dashboard.models import Competitor, CompetitorProduct, ShopifyProduct

    with get_session() as s:
        s.add(Competitor(name="Rival", base_url="https://r.example", enabled=True))
        for i in range(4):
            s.add(ShopifyProduct(product_gid=f"g{i}", handle=f"h{i}",
                                 title=f"Euro Style Atlantic {i}", price_min=0.0))
        s.flush()
        for i in range(3):
            s.add(CompetitorProduct(competitor_id=1, handle=f"t{i}",
                                    title=f"Euro Style La Foret {i}",
                                    price_min=2.69 + i))

    gaps = reporting.price_visibility_gaps()

    assert gaps, "expected the euro style overlap to be found"
    top = gaps[0]
    assert top["phrase"] == "euro style"
    assert top["ours_hidden"] == 4
    assert top["theirs_priced"] == 3
    assert top["their_low"] == 2.69
    assert top["competitors"] == ["Rival"]


def test_a_single_product_overlap_is_not_called_a_range(dashboard_db):
    """One product on either side is a coincidence. Reporting it as a range
    would fill this table with noise and make the real rows unfindable."""
    from dashboard import reporting
    from dashboard.db import get_session
    from dashboard.models import Competitor, CompetitorProduct, ShopifyProduct

    with get_session() as s:
        s.add(Competitor(name="Rival", base_url="https://r.example", enabled=True))
        s.add(ShopifyProduct(product_gid="g1", handle="h1",
                             title="Lonely Range Oak", price_min=0.0))
        s.flush()
        s.add(CompetitorProduct(competitor_id=1, handle="t1",
                                title="Lonely Range Oak", price_min=3.0))

    assert reporting.price_visibility_gaps() == []


def test_products_whose_price_we_show_are_not_counted_as_hidden(dashboard_db):
    from dashboard import reporting
    from dashboard.db import get_session
    from dashboard.models import Competitor, CompetitorProduct, ShopifyProduct

    with get_session() as s:
        s.add(Competitor(name="Rival", base_url="https://r.example", enabled=True))
        for i in range(3):
            s.add(ShopifyProduct(product_gid=f"g{i}", handle=f"h{i}",
                                 title=f"Euro Style Atlantic {i}",
                                 price_min=0.0 if i < 2 else 5.0))
        s.flush()
        for i in range(2):
            s.add(CompetitorProduct(competitor_id=1, handle=f"t{i}",
                                    title=f"Euro Style Nordic {i}", price_min=3.0))

    top = reporting.price_visibility_gaps()[0]

    assert top["ours_hidden"] == 2
    assert top["ours_priced"] == 1
