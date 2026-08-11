"""Watching competitors.

Everything here reads other people's public pages, and the failure mode that
matters is not "it crashed" — it's "it produced a plausible number that is
wrong". A sales ranking that's really alphabetical order, a price comparison
against the wrong product, a hidden price counted as $0: each looks like
data and each would be acted on. Most of these tests exist for that.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from dashboard import competitors as fetchers
from dashboard import matching
from dashboard.db import get_session
from dashboard.models import (
    Competitor,
    CompetitorMatch,
    CompetitorPost,
    CompetitorProduct,
    MatchStatus,
    ShopifyProduct,
)

TODAY = date(2026, 8, 9)
NOW = datetime(2026, 8, 9, 12, 0)


# ── Reading their site ──────────────────────────────────────────────


class _Resp:
    def __init__(self, text="", status=200, payload=None):
        self.text = text
        self.status_code = status
        self._payload = payload
        self.content = text.encode() if text else b"{}"

    def json(self):
        return self._payload if self._payload is not None else {}


class _Client:
    """Serves canned responses by URL substring."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None):
        self.calls.append(url)
        for fragment, resp in self.routes.items():
            if fragment in url:
                return resp
        return _Resp(status=404)

    def close(self):
        pass


def test_alphabetical_bestsellers_are_refused_not_recorded():
    """`/collections/all/products.json` silently ignores sort_by — confirmed
    against a live store, where best-selling, title-ascending and no sort at
    all returned byte-identical results. Recording that as a sales ranking
    would be worse than having none, because it looks like data. The HTML
    page honours the sort, but some themes don't, so the alphabetical shape
    is refused wherever it comes from."""
    html = "".join(
        f'<a href="/products/{h}">x</a>' for h in ["aaa-oak", "bbb-oak", "ccc-oak"]
    )
    client = _Client({"/collections/all": _Resp(html)})

    with pytest.raises(fetchers.FetchError, match="alphabetical"):
        fetchers.fetch_shopify_bestsellers("https://x.example", client=client)


def test_a_real_ranking_is_kept_in_order():
    html = "".join(
        f'<a href="/products/{h}"><img></a><a href="/products/{h}">t</a>'
        for h in ["zzz-popular", "aaa-second", "mmm-third"]
    )
    client = _Client({"/collections/all": _Resp(html)})

    ranked = fetchers.fetch_shopify_bestsellers("https://x.example", client=client)

    assert ranked == ["zzz-popular", "aaa-second", "mmm-third"]


def test_feed_entities_are_decoded():
    """Jinja escapes on output, so an entity left in the stored title renders
    literally as `&amp;` on the page. Seen for real on a live feed."""
    atom = """
    <feed><entry>
      <title>Laminate &amp; Vinyl Flooring</title>
      <link href="https://x.example/a"/>
      <published>2026-08-01T06:37:04Z</published>
    </entry></feed>
    """
    posts = fetchers.parse_feed(atom)

    assert len(posts) == 1
    assert posts[0].title == "Laminate & Vinyl Flooring"
    assert posts[0].published_at == datetime(2026, 8, 1, 6, 37, 4)


def test_rss_dates_are_parsed_too():
    """RSS uses RFC 822, which fromisoformat won't touch — a post with no
    date sorts to the bottom and looks like it was never published."""
    rss = """
    <rss><channel><item>
      <title>Stair nosing</title>
      <link>https://x.example/b</link>
      <pubDate>Tue, 05 Aug 2026 10:00:00 GMT</pubDate>
    </item></channel></rss>
    """
    posts = fetchers.parse_feed(rss)

    assert posts[0].published_at == datetime(2026, 8, 5, 10, 0)


def test_a_malformed_feed_still_yields_what_it_can():
    """These come from other people's servers and are frequently slightly
    broken. A strict XML parser turns one stray tag into "this competitor
    has no blog"."""
    broken = """
    <feed><entry><title>Good one</title><link href="https://x.example/c"/></entry>
    <entry><title>No link here</title></entry>
    <entry <broken
    """
    posts = fetchers.parse_feed(broken)

    assert [p.title for p in posts] == ["Good one"]


# ── Matching ────────────────────────────────────────────────────────


def test_the_same_board_phrased_differently_matches():
    ours = matching.signature(
        "Euro Style German Laminate Atlantic 12mm Appalachian Hickory", "Euro Style"
    )
    theirs = matching.signature(
        "Euro Style Atlantic 12mm Laminate - Appalachian Hickory", "Euro Style"
    )
    value, reasons = matching.score(ours, theirs)

    assert value >= matching.MIN_SCORE
    assert any("thickness" in r for r in reasons)


def test_a_thickness_mismatch_defeats_a_near_identical_name():
    """8mm and 12mm laminate are not the same product however similar the
    names, and this is the pair a word-overlap matcher gets wrong: four
    shared words and a different board."""
    ours = matching.signature("Euro Style Atlantic 12mm Appalachian Hickory")
    theirs = matching.signature("Euro Style Atlantic 8mm Appalachian Hickory")

    value, reasons = matching.score(ours, theirs)

    assert value < matching.MIN_SCORE
    assert any("different thickness" in r for r in reasons)


def test_unrelated_products_score_near_zero():
    ours = matching.signature("Coastal Oak Vinyl Plank 20mil")
    theirs = matching.signature("Stair Nose Moulding Oak")

    value, _ = matching.score(ours, theirs)

    assert value < matching.MIN_SCORE


def test_only_the_best_candidate_is_proposed_per_product():
    """A review queue with six near-identical options for one product is a
    queue nobody finishes."""

    class P:
        def __init__(self, pid, title):
            self.id, self.title, self.vendor = pid, title, None

    ours = [
        P(1, "Atlantic 12mm Hickory Laminate"),
        P(2, "Atlantic 12mm Hickory Laminate Plank"),
        P(3, "Atlantic 12mm Hickory Flooring"),
    ]
    theirs = [P(9, "Atlantic 12mm Hickory")]

    proposals = matching.propose(ours, theirs)

    assert len(proposals) == 1
    assert proposals[0][0] == 9


# ── Rotation and starvation ─────────────────────────────────────────


def test_a_site_that_always_fails_does_not_starve_the_others(
    dashboard_db, monkeypatch
):
    """One competitor per run, stalest first. If the attempt weren't stamped
    before the fetch, a permanently unreachable site would keep a null
    timestamp, sort first forever, and no other competitor would ever be
    collected."""
    from dashboard.jobs import competitor_watch as watch

    with get_session() as session:
        session.add(Competitor(name="Broken", base_url="https://broken.example"))
        session.add(Competitor(name="Fine", base_url="https://fine.example"))

    monkeypatch.setattr(
        fetchers, "probe_platform",
        lambda base, client=None: (_ for _ in ()).throw(
            fetchers.FetchError("unreachable")
        ),
    )

    first = watch.sync_competitor_catalog(today=TODAY)
    second = watch.sync_competitor_catalog(today=TODAY)

    assert first.skipped and second.skipped
    # Two different competitors were attempted, not the same one twice.
    assert first.detail["competitor"] != second.detail["competitor"]


def test_a_failure_reason_is_recorded_on_the_competitor(dashboard_db, monkeypatch):
    """So the page can say "this one is not readable" next to the competitor
    it's about, rather than only in a job log nobody opens.

    A non-Shopify site is no longer a dead end — it falls through to the
    sitemap path — so what's asserted here is that whatever *does* go wrong
    lands on the competitor row, not the particular thing that went wrong.
    """
    from dashboard.jobs import competitor_watch as watch

    with get_session() as session:
        session.add(Competitor(name="Wordpress Co", base_url="https://wp.example"))

    monkeypatch.setattr(fetchers, "probe_platform", lambda base, client=None: "other")
    # No network from the test suite: wp.example doesn't resolve, and a test
    # whose speed depends on a DNS timeout is a test that flakes.
    monkeypatch.setattr(
        fetchers, "fetch_generic_catalog",
        lambda *a, **k: (_ for _ in ()).throw(
            fetchers.FetchError("No sitemap found.")
        ),
    )

    result = watch.sync_competitor_catalog(today=TODAY)

    assert result.skipped
    with get_session() as session:
        row = session.query(Competitor).one()
        assert "No sitemap found." in row.last_error
        assert row.last_checked_at is not None


def test_no_competitors_is_a_skip_not_a_failure(dashboard_db):
    from dashboard.jobs import competitor_watch as watch

    result = watch.sync_competitor_catalog(today=TODAY)

    assert result.skipped is True
    assert "Settings" in result.skip_reason


# ── Price comparison ────────────────────────────────────────────────


def _seed_match(session, *, our_price: float, their_price: float, status: str):
    session.add(Competitor(id=1, name="Rival", base_url="https://r.example"))
    session.add(ShopifyProduct(
        id=1, product_gid="gid://1", handle="ours", title="Atlantic 12mm",
        price_min=our_price,
    ))
    session.add(CompetitorProduct(
        id=1, competitor_id=1, handle="theirs", title="Atlantic 12mm",
        price_min=their_price,
    ))
    session.flush()
    session.add(CompetitorMatch(
        competitor_product_id=1, shopify_product_id=1, status=status, score=0.9,
    ))


def test_only_confirmed_matches_reach_the_price_table(dashboard_db):
    """A proposed match is a guess, and a guess in a price table becomes
    "they're undercutting us on X" about a product that isn't X."""
    from dashboard import reporting

    with get_session() as session:
        _seed_match(session, our_price=3.99, their_price=2.89,
                    status=MatchStatus.proposed.value)

    data = reporting.competitors(days=90, today=TODAY)

    assert data["comparisons"] == []
    assert len(data["queue"]) == 1


def test_a_hidden_price_is_not_reported_as_winning(dashboard_db):
    """~94% of this catalogue sits at 0.00 with the price hidden by an app.
    Subtracting naively would report every one of them as us being $2.89
    cheaper, which is the opposite of the truth."""
    from dashboard import reporting

    with get_session() as session:
        _seed_match(session, our_price=0.0, their_price=2.89,
                    status=MatchStatus.confirmed.value)

    row = reporting.competitors(days=90, today=TODAY)["comparisons"][0]

    assert row["our_price_hidden"] is True
    assert row["delta"] is None
    assert row["we_are_higher"] is False


def test_being_more_expensive_is_flagged(dashboard_db):
    from dashboard import reporting

    with get_session() as session:
        _seed_match(session, our_price=3.99, their_price=2.89,
                    status=MatchStatus.confirmed.value)

    row = reporting.competitors(days=90, today=TODAY)["comparisons"][0]

    assert row["delta"] == pytest.approx(1.10)
    assert row["we_are_higher"] is True


# ── Alerts ──────────────────────────────────────────────────────────


def test_an_undercut_alert_needs_a_confirmed_match(dashboard_db):
    from dashboard import alerts

    with get_session() as session:
        _seed_match(session, our_price=3.99, their_price=2.00,
                    status=MatchStatus.proposed.value)

    assert alerts._competitor_undercuts(10, TODAY) == []

    with get_session() as session:
        session.query(CompetitorMatch).one().status = MatchStatus.confirmed.value

    findings = alerts._competitor_undercuts(10, TODAY)
    assert len(findings) == 1
    assert "cheaper" in findings[0].title


def test_a_hidden_price_never_raises_an_undercut_alert(dashboard_db):
    """Hidden is not expensive and it is not cheap — it's unknown, and an
    alert about an unknown is noise."""
    from dashboard import alerts

    with get_session() as session:
        _seed_match(session, our_price=0.0, their_price=2.00,
                    status=MatchStatus.confirmed.value)

    assert alerts._competitor_undercuts(10, TODAY) == []


def test_a_busy_week_produces_one_alert_per_competitor(dashboard_db):
    """Five alerts because they had a busy week is how an inbox gets
    ignored."""
    from dashboard import alerts

    with get_session() as session:
        session.add(Competitor(id=1, name="Rival", base_url="https://r.example"))
        session.flush()
        for i in range(5):
            session.add(CompetitorPost(
                competitor_id=1, url=f"https://r.example/{i}", title=f"Post {i}",
                published_at=NOW - timedelta(days=i),
            ))

    findings = alerts._competitor_posted(7, TODAY)

    assert len(findings) == 1
    assert "5 posts" in findings[0].title


def test_ranking_nothing_is_reported_as_a_skip_not_a_success(
    dashboard_db, monkeypatch
):
    """Every handle in their best-seller list absent from our catalogue copy
    means the catalogue sync hasn't run for them yet — a different problem
    with a different fix than "nothing to do". Hit for real: the job returned
    `ok, 0 rows` and the page's best-seller section silently stayed empty."""
    from dashboard.jobs import competitor_watch as watch

    with get_session() as session:
        session.add(Competitor(
            name="Rival", base_url="https://r.example", platform="shopify",
        ))

    monkeypatch.setattr(
        fetchers, "fetch_shopify_bestsellers",
        lambda base, client=None: ["never-synced-a", "never-synced-b"],
    )

    result = watch.sync_competitor_bestsellers(today=TODAY)

    assert result.skipped is True
    assert "catalogue snapshot" in result.skip_reason
    assert result.detail["in_list_but_not_in_catalogue"] == 2


def test_ranking_products_we_do_have_succeeds(dashboard_db, monkeypatch):
    from dashboard.jobs import competitor_watch as watch

    with get_session() as session:
        session.add(Competitor(
            id=1, name="Rival", base_url="https://r.example", platform="shopify",
        ))
        session.flush()
        for handle in ("top-seller", "second"):
            session.add(CompetitorProduct(
                competitor_id=1, handle=handle, title=handle, price_min=1.0,
            ))

    monkeypatch.setattr(
        fetchers, "fetch_shopify_bestsellers",
        lambda base, client=None: ["top-seller", "second"],
    )

    result = watch.sync_competitor_bestsellers(today=TODAY)

    assert result.skipped is False
    assert result.rows == 2
    with get_session() as session:
        best = (
            session.query(CompetitorProduct)
            .filter(CompetitorProduct.handle == "top-seller")
            .one()
        )
        assert best.best_seller_rank == 1
