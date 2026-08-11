"""Read-side queries. Every page in the UI is built from this module.

Two arithmetic rules are enforced here rather than left to each caller,
because getting either wrong produces numbers that look plausible and are
wrong:

  * **CTR is recomputed, never averaged.** The mean of daily CTRs weights a
    day with nine impressions the same as a day with nine thousand. Site CTR
    is total clicks over total impressions, always.
  * **Position is impression-weighted.** Search Console's own average position
    is weighted that way, so an unweighted mean of daily positions silently
    disagrees with the number the owner sees in Google's UI — the fastest way
    to lose trust in a dashboard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func

from dashboard.db import get_session
from dashboard.jobs.dataforseo_keywords import STRIKING_MAX, STRIKING_MIN
from dashboard.jobs.gsc import settled_through
from dashboard.models import (
    AdsCampaignDaily,
    BlogArticle,
    Competitor,
    CompetitorMatch,
    CompetitorPost,
    CompetitorProduct,
    CompetitorProductPrice,
    Ga4EventDaily,
    GscPageDaily,
    GscQueryDaily,
    GscSiteDaily,
    KeywordMetric,
    Experiment,
    ExperimentProduct,
    MatchStatus,
    ShopifyProduct,
)


@dataclass(frozen=True)
class Metrics:
    clicks: int = 0
    impressions: int = 0
    # Impression-weighted sum of positions, kept so windows can be combined.
    position_weight: float = 0.0

    @property
    def ctr(self) -> float:
        return self.clicks / self.impressions if self.impressions else 0.0

    @property
    def position(self) -> float:
        return self.position_weight / self.impressions if self.impressions else 0.0


@dataclass(frozen=True)
class Delta:
    """A metric now versus the equivalent earlier window."""

    label: str
    current: float
    previous: float
    # True when a *lower* number is the better outcome — search position is
    # the only one here, and rendering it like the rest would paint a genuine
    # improvement red.
    lower_is_better: bool = False
    kind: str = "int"  # int | pct | float

    @property
    def change(self) -> float:
        return self.current - self.previous

    @property
    def change_pct(self) -> float | None:
        if not self.previous:
            return None
        return (self.current - self.previous) / self.previous * 100.0

    @property
    def direction(self) -> str:
        """"up" / "down" / "flat" — visual direction, not value direction."""
        if abs(self.change) < 1e-9:
            return "flat"
        return "up" if self.change > 0 else "down"

    @property
    def good(self) -> bool | None:
        if self.direction == "flat":
            return None
        rising = self.direction == "up"
        return not rising if self.lower_is_better else rising


@dataclass(frozen=True)
class DayPoint:
    day: date
    clicks: int
    impressions: int
    ctr: float
    position: float
    # Inside Search Console's settling window: real, but Google may restate it.
    provisional: bool


def _window(end: date, days: int) -> tuple[date, date]:
    return end - timedelta(days=days - 1), end


def _metrics(session, start: date, end: date) -> Metrics:
    row = session.query(
        func.coalesce(func.sum(GscSiteDaily.clicks), 0),
        func.coalesce(func.sum(GscSiteDaily.impressions), 0),
        func.coalesce(
            func.sum(GscSiteDaily.position * GscSiteDaily.impressions), 0.0
        ),
    ).filter(GscSiteDaily.date >= start, GscSiteDaily.date <= end).one()
    return Metrics(clicks=int(row[0]), impressions=int(row[1]),
                   position_weight=float(row[2]))


def site_series(days: int = 90, today: date | None = None) -> list[DayPoint]:
    """One point per day, oldest first, for the trend chart."""
    today = today or date.today()
    start = today - timedelta(days=days - 1)
    cutoff = settled_through(today)
    with get_session() as session:
        rows = (
            session.query(GscSiteDaily)
            .filter(GscSiteDaily.date >= start)
            .order_by(GscSiteDaily.date.asc())
            .all()
        )
    return [
        DayPoint(
            day=r.date,
            clicks=r.clicks,
            impressions=r.impressions,
            ctr=r.ctr,
            position=r.position,
            provisional=r.date > cutoff,
        )
        for r in rows
    ]


def site_summary(window_days: int = 28, today: date | None = None) -> dict:
    """Current window vs the window immediately before it.

    Both windows end on settled data. Comparing a window whose last three days
    are still being restated against a fully settled one manufactures a
    decline every single time, which is the classic way a dashboard cries wolf.
    """
    today = today or date.today()
    end = settled_through(today)
    cur_start, cur_end = _window(end, window_days)
    prev_start, prev_end = _window(cur_start - timedelta(days=1), window_days)

    with get_session() as session:
        current = _metrics(session, cur_start, cur_end)
        previous = _metrics(session, prev_start, prev_end)
        latest = session.query(func.max(GscSiteDaily.date)).scalar()
        earliest = session.query(func.min(GscSiteDaily.date)).scalar()

    deltas = [
        Delta("Clicks", current.clicks, previous.clicks),
        Delta("Impressions", current.impressions, previous.impressions),
        Delta("CTR", current.ctr * 100, previous.ctr * 100, kind="pct"),
        Delta(
            "Avg position",
            current.position,
            previous.position,
            lower_is_better=True,
            kind="float",
        ),
    ]
    return {
        "window_days": window_days,
        "current_window": (cur_start, cur_end),
        "previous_window": (prev_start, prev_end),
        "current": current,
        "previous": previous,
        "deltas": deltas,
        "latest_day": latest,
        "earliest_day": earliest,
        "settled_through": end,
        "has_data": current.impressions > 0 or previous.impressions > 0,
    }


def top_pages(
    *,
    window_days: int = 28,
    limit: int = 25,
    order: str = "clicks",
    today: date | None = None,
) -> list[dict]:
    """Best pages in the settled window, with the previous window alongside.

    The comparison column is what makes the list actionable: a page with 400
    clicks is only interesting next to whether it had 200 or 900 last month.
    """
    today = today or date.today()
    end = settled_through(today)
    cur_start, cur_end = _window(end, window_days)
    prev_start, prev_end = _window(cur_start - timedelta(days=1), window_days)
    order_col = {
        "clicks": func.sum(GscPageDaily.clicks),
        "impressions": func.sum(GscPageDaily.impressions),
    }.get(order, func.sum(GscPageDaily.clicks))

    def aggregate(session, start: date, stop: date):
        return dict(
            (page, (int(clicks), int(impressions), float(pos_weight)))
            for page, clicks, impressions, pos_weight in session.query(
                GscPageDaily.page,
                func.coalesce(func.sum(GscPageDaily.clicks), 0),
                func.coalesce(func.sum(GscPageDaily.impressions), 0),
                func.coalesce(
                    func.sum(GscPageDaily.position * GscPageDaily.impressions), 0.0
                ),
            )
            .filter(GscPageDaily.date >= start, GscPageDaily.date <= stop)
            .group_by(GscPageDaily.page)
            .all()
        )

    with get_session() as session:
        top = (
            session.query(
                GscPageDaily.page,
                func.coalesce(func.sum(GscPageDaily.clicks), 0),
                func.coalesce(func.sum(GscPageDaily.impressions), 0),
                func.coalesce(
                    func.sum(GscPageDaily.position * GscPageDaily.impressions), 0.0
                ),
            )
            .filter(GscPageDaily.date >= cur_start, GscPageDaily.date <= cur_end)
            .group_by(GscPageDaily.page)
            .order_by(order_col.desc())
            .limit(limit)
            .all()
        )
        pages = [row[0] for row in top]
        prev = aggregate(session, prev_start, prev_end) if pages else {}

    out = []
    for page, clicks, impressions, pos_weight in top:
        cur = Metrics(int(clicks), int(impressions), float(pos_weight))
        p_clicks, p_impr, p_weight = prev.get(page, (0, 0, 0.0))
        was = Metrics(p_clicks, p_impr, p_weight)
        out.append(
            {
                "page": page,
                "current": cur,
                "previous": was,
                "clicks_delta": cur.clicks - was.clicks,
                "impressions_delta": cur.impressions - was.impressions,
                "position_delta": (
                    cur.position - was.position if was.impressions else None
                ),
            }
        )
    return out


def normalize_url(url: str | None) -> str:
    """A URL reduced to host+path, lowercased, without scheme, www or slash.

    Search Console reports the canonical URL, which will not match a string we
    built ourselves character for character. A join that silently matches
    nothing looks exactly like a catalogue with no search traffic, which is
    the most convincing way to be wrong.
    """
    if not url:
        return ""
    u = str(url).strip().lower()
    for prefix in ("https://", "http://"):
        if u.startswith(prefix):
            u = u[len(prefix):]
    if u.startswith("www."):
        u = u[4:]
    return u.split("?", 1)[0].split("#", 1)[0].rstrip("/")


def _page_metrics(session, start: date, end: date) -> dict[str, Metrics]:
    """Per-URL metrics for a window, keyed by normalised URL."""
    out: dict[str, Metrics] = {}
    rows = (
        session.query(
            GscPageDaily.page,
            func.coalesce(func.sum(GscPageDaily.clicks), 0),
            func.coalesce(func.sum(GscPageDaily.impressions), 0),
            func.coalesce(
                func.sum(GscPageDaily.position * GscPageDaily.impressions), 0.0
            ),
        )
        .filter(GscPageDaily.date >= start, GscPageDaily.date <= end)
        .group_by(GscPageDaily.page)
        .all()
    )
    for page, clicks, impressions, weight in rows:
        key = normalize_url(page)
        if not key:
            continue
        # Google can report the same page under more than one canonical form
        # (trailing slash, query string); normalising collapses them, so add
        # rather than overwrite or the second form would erase the first.
        prior = out.get(key)
        merged = Metrics(int(clicks), int(impressions), float(weight))
        out[key] = (
            merged if prior is None
            else Metrics(
                prior.clicks + merged.clicks,
                prior.impressions + merged.impressions,
                prior.position_weight + merged.position_weight,
            )
        )
    return out


def products(
    *,
    window_days: int = 28,
    search: str = "",
    status: str = "",
    priced: str = "",
    experiment: str = "",
    cohort: str = "",
    order: str = "clicks",
    limit: int = 50,
    offset: int = 0,
    today: date | None = None,
) -> dict:
    """Catalogue rows joined to their search metrics.

    The join happens in Python rather than SQL because the keys need
    normalising on both sides and SQLite has no expression index to make that
    cheap. At ~2,800 products against one window's page aggregates that is a
    few milliseconds, and it keeps the normalisation in exactly one function
    instead of duplicated into a query.
    """
    today = today or date.today()
    end = settled_through(today)
    cur_start, cur_end = _window(end, window_days)
    prev_start, prev_end = _window(cur_start - timedelta(days=1), window_days)

    with get_session() as session:
        current = _page_metrics(session, cur_start, cur_end)
        previous = _page_metrics(session, prev_start, prev_end)

        query = session.query(ShopifyProduct)
        if search:
            like = f"%{search.strip()}%"
            query = query.filter(
                ShopifyProduct.title.ilike(like) | ShopifyProduct.handle.ilike(like)
            )
        if status:
            query = query.filter(ShopifyProduct.status == status)
        if priced == "yes":
            query = query.filter(ShopifyProduct.price_min > 0)
        elif priced == "no":
            query = query.filter(ShopifyProduct.price_min <= 0)

        members: dict[str, str] = {}
        if experiment:
            member_rows = (
                session.query(
                    ExperimentProduct.product_gid, ExperimentProduct.cohort
                )
                .filter(ExperimentProduct.experiment_id == int(experiment))
                .all()
            )
            members = dict(member_rows)
            if cohort:
                keep = {g for g, c in members.items() if c == cohort}
                query = query.filter(ShopifyProduct.product_gid.in_(keep or [""]))

        rows = query.all()
        for row in rows:
            session.expunge(row)

        experiments = [
            {"id": e.id, "name": e.name}
            for e in session.query(Experiment).order_by(Experiment.id.desc()).all()
        ]
        statuses = [
            s for (s,) in session.query(ShopifyProduct.status).distinct().all() if s
        ]

    zero = Metrics()
    joined = []
    for product in rows:
        key = normalize_url(product.online_url)
        now = current.get(key, zero)
        was = previous.get(key, zero)
        joined.append({
            "product": product,
            "current": now,
            "previous": was,
            "clicks_delta": now.clicks - was.clicks,
            "impressions_delta": now.impressions - was.impressions,
            "cohort": members.get(product.product_gid),
        })

    keys = {
        "clicks": lambda r: (-r["current"].clicks, -r["current"].impressions),
        "impressions": lambda r: -r["current"].impressions,
        "movers": lambda r: -r["clicks_delta"],
        "fallers": lambda r: r["clicks_delta"],
        # Products with no impressions have position 0, which would sort as
        # rank 1 — the best possible. Push them to the back explicitly.
        "position": lambda r: (
            r["current"].position if r["current"].impressions else 1e9
        ),
        "title": lambda r: r["product"].title.lower(),
        "price": lambda r: -r["product"].price_min,
    }
    joined.sort(key=keys.get(order, keys["clicks"]))

    total = len(joined)
    page = joined[offset:offset + limit]
    return {
        "rows": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "window": (cur_start, cur_end),
        "previous_window": (prev_start, prev_end),
        "with_traffic": sum(1 for r in joined if r["current"].impressions > 0),
        "experiments": experiments,
        "statuses": sorted(statuses),
    }


def blog_posts(
    *,
    window_days: int = 28,
    order: str = "decay",
    today: date | None = None,
) -> dict:
    """Articles ranked by measured decay, with refresh history alongside.

    Decay is **absolute impressions lost**, not percent — the same choice the
    refresh pipeline makes, and for the same reason. Percentage flatters
    trivia: a post falling 25→1 is a 96% collapse worth 24 impressions, while
    18,272→5,497 is "only" −70% and worth 12,775. The second one is the
    article to rewrite.

    What's different here is the input. `run-refresh` compares two 90-day
    `search_performance` snapshots; this compares two adjacent windows built
    from daily rows, so the window is selectable and the two halves never
    overlap.
    """
    today = today or date.today()
    end = settled_through(today)
    cur_start, cur_end = _window(end, window_days)
    prev_start, prev_end = _window(cur_start - timedelta(days=1), window_days)

    with get_session() as session:
        current = _page_metrics(session, cur_start, cur_end)
        previous = _page_metrics(session, prev_start, prev_end)
        articles = session.query(BlogArticle).all()
        for a in articles:
            session.expunge(a)

    zero = Metrics()
    rows = []
    for article in articles:
        key = normalize_url(article.shopify_url)
        now = current.get(key, zero)
        was = previous.get(key, zero)
        lost = was.impressions - now.impressions
        rows.append({
            "article": article,
            "current": now,
            "previous": was,
            "clicks_delta": now.clicks - was.clicks,
            "impressions_lost": lost,
            # Only call it decay when there's a real baseline to fall from.
            # A post that never had impressions isn't decaying, it's new or
            # it's invisible, and those want different responses.
            "decaying": lost > 0 and was.impressions >= 50,
            "live": bool(article.shopify_url),
        })

    keys = {
        "decay": lambda r: -r["impressions_lost"],
        "clicks": lambda r: -r["current"].clicks,
        "impressions": lambda r: -r["current"].impressions,
        "refreshed": lambda r: (
            r["article"].last_refreshed_at is not None,
            r["article"].last_refreshed_at or datetime.min,
        ),
        "published": lambda r: (
            r["article"].published_at is None,
            # Newest first among those that have a date.
            -(r["article"].published_at or datetime.min).timestamp()
            if r["article"].published_at else 0,
        ),
    }
    rows.sort(key=keys.get(order, keys["decay"]))

    return {
        "rows": rows,
        "window": (cur_start, cur_end),
        "previous_window": (prev_start, prev_end),
        "total": len(rows),
        "live": sum(1 for r in rows if r["live"]),
        "decaying": sum(1 for r in rows if r["decaying"]),
        "ever_refreshed": sum(
            1 for r in rows if r["article"].revision_count
        ),
        "total_cost": round(sum(r["article"].cost_usd for r in rows), 4),
    }


def article_detail(
    article_id: int, *, window_days: int = 28, today: date | None = None
) -> dict | None:
    """One article's numbers, plus a daily series for its own trend chart."""
    today = today or date.today()
    end = settled_through(today)
    cur_start, cur_end = _window(end, window_days)
    prev_start, prev_end = _window(cur_start - timedelta(days=1), window_days)

    with get_session() as session:
        article = (
            session.query(BlogArticle)
            .filter(BlogArticle.pipeline_id == article_id)
            .one_or_none()
        )
        if article is None:
            return None
        session.expunge(article)
        current = _page_metrics(session, cur_start, cur_end)
        previous = _page_metrics(session, prev_start, prev_end)

        key = normalize_url(article.shopify_url)
        series: list[DayPoint] = []
        if key:
            rows = (
                session.query(GscPageDaily)
                .filter(
                    GscPageDaily.date >= today - timedelta(days=179),
                    GscPageDaily.page.isnot(None),
                )
                .order_by(GscPageDaily.date.asc())
                .all()
            )
            cutoff = settled_through(today)
            by_day: dict[date, list] = {}
            for row in rows:
                if normalize_url(row.page) != key:
                    continue
                by_day.setdefault(row.date, []).append(row)
            for day in sorted(by_day):
                items = by_day[day]
                impressions = sum(i.impressions for i in items)
                series.append(DayPoint(
                    day=day,
                    clicks=sum(i.clicks for i in items),
                    impressions=impressions,
                    ctr=0.0,
                    position=(
                        sum(i.position * i.impressions for i in items) / impressions
                        if impressions else 0.0
                    ),
                    provisional=day > cutoff,
                ))

    now = current.get(key, Metrics())
    was = previous.get(key, Metrics())
    return {
        "article": article,
        "current": now,
        "previous": was,
        "clicks_delta": now.clicks - was.clicks,
        "impressions_lost": was.impressions - now.impressions,
        "decaying": (was.impressions - now.impressions) > 0 and was.impressions >= 50,
        "series": series,
        "window": (cur_start, cur_end),
        "previous_window": (prev_start, prev_end),
    }


def ads_overview(*, window_days: int = 28, today: date | None = None) -> dict:
    """Paid spend against the outcome it is supposed to produce.

    The comparison this whole module exists for: Google Ads reports its own
    conversions, but the store's real result is a phone call recorded in GA4,
    and organic search delivers those calls too. Showing paid spend beside the
    organic clicks and the total call events for identical dates is the view
    neither Google Ads nor Analytics gives on its own.

    An honest caveat travels with it, in `attribution_note`: nothing here
    attributes a specific call to a specific campaign. These are totals over
    the same window, not a funnel.
    """
    today = today or date.today()
    end = settled_through(today)
    cur_start, cur_end = _window(end, window_days)
    prev_start, prev_end = _window(cur_start - timedelta(days=1), window_days)

    def paid(session, start: date, stop: date):
        return session.query(
            func.coalesce(func.sum(AdsCampaignDaily.spend), 0.0),
            func.coalesce(func.sum(AdsCampaignDaily.clicks), 0),
            func.coalesce(func.sum(AdsCampaignDaily.impressions), 0),
            func.coalesce(func.sum(AdsCampaignDaily.conversions), 0.0),
        ).filter(
            AdsCampaignDaily.date >= start, AdsCampaignDaily.date <= stop
        ).one()

    def events(session, start: date, stop: date) -> int:
        return int(session.query(
            func.coalesce(func.sum(Ga4EventDaily.event_count), 0)
        ).filter(
            Ga4EventDaily.date >= start, Ga4EventDaily.date <= stop
        ).scalar() or 0)

    with get_session() as session:
        spend, clicks, impressions, conversions = paid(session, cur_start, cur_end)
        p_spend, p_clicks, _, p_conversions = paid(session, prev_start, prev_end)
        calls = events(session, cur_start, cur_end)
        prev_calls = events(session, prev_start, prev_end)
        organic = _metrics(session, cur_start, cur_end)
        # The GTM conversion tags on this store only started firing partway
        # through 2026-07. Comparing against a window that predates them
        # produces a spectacular percentage rise that measures when the
        # tracking was installed, not the business.
        first_event_day = session.query(func.min(Ga4EventDaily.date)).scalar()

        campaigns = [
            {
                "campaign": name,
                "campaign_type": ctype,
                "status": status,
                "spend": float(c_spend),
                "clicks": int(c_clicks),
                "impressions": int(c_impr),
                "conversions": float(c_conv),
                "cpc": (float(c_spend) / int(c_clicks)) if c_clicks else None,
                "cost_per_conversion": (
                    (float(c_spend) / float(c_conv)) if c_conv else None
                ),
            }
            for name, ctype, status, c_spend, c_clicks, c_impr, c_conv in session.query(
                AdsCampaignDaily.campaign,
                func.max(AdsCampaignDaily.campaign_type),
                func.max(AdsCampaignDaily.campaign_status),
                func.coalesce(func.sum(AdsCampaignDaily.spend), 0.0),
                func.coalesce(func.sum(AdsCampaignDaily.clicks), 0),
                func.coalesce(func.sum(AdsCampaignDaily.impressions), 0),
                func.coalesce(func.sum(AdsCampaignDaily.conversions), 0.0),
            )
            .filter(
                AdsCampaignDaily.date >= cur_start, AdsCampaignDaily.date <= cur_end
            )
            .group_by(AdsCampaignDaily.campaign)
            .order_by(func.sum(AdsCampaignDaily.spend).desc())
            .all()
        ]

    spend = float(spend)
    return {
        "window": (cur_start, cur_end),
        "previous_window": (prev_start, prev_end),
        "has_data": bool(campaigns),
        "spend": spend,
        "clicks": int(clicks),
        "impressions": int(impressions),
        "conversions": float(conversions),
        "cpc": (spend / int(clicks)) if clicks else None,
        "cost_per_conversion": (spend / float(conversions)) if conversions else None,
        "calls": calls,
        "organic_clicks": organic.clicks,
        "campaigns": campaigns,
        "calls_baseline_incomplete": bool(
            first_event_day and first_event_day > prev_start
        ),
        "first_event_day": first_event_day,
        "deltas": [
            Delta("Spend", spend, float(p_spend), lower_is_better=True, kind="float"),
            Delta("Paid clicks", int(clicks), int(p_clicks)),
            Delta("Ads conversions", float(conversions), float(p_conversions),
                  kind="float"),
            Delta("Call + WhatsApp events", calls, prev_calls),
        ],
        "attribution_note": (
            "Totals over the same dates, not a funnel. Nothing here ties a "
            "specific call to a specific campaign — Google Ads counts its own "
            "conversions, GA4 counts the click on the phone number, and the "
            "two are measured by different systems with different windows."
        ),
    }


def keywords(
    *,
    window_days: int = 28,
    order: str = "opportunity",
    striking_only: bool = True,
    search: str = "",
    limit: int = 100,
    today: date | None = None,
) -> dict:
    """Search terms joined to their market data.

    Search Console says how the site does on a term. DataForSEO says how big
    the term is and what a click is worth. Neither is actionable alone: a term
    with 40,000 searches the site ranks 90th for is a fantasy, and a term the
    site ranks 6th for that nobody searches is a rounding error. Together they
    rank real opportunity.
    """
    today = today or date.today()
    end = settled_through(today)
    start = end - timedelta(days=window_days - 1)
    prev_start = start - timedelta(days=window_days)
    prev_end = start - timedelta(days=1)

    def aggregate(session, begin: date, finish: date) -> dict[str, Metrics]:
        rows = (
            session.query(
                GscQueryDaily.query,
                func.coalesce(func.sum(GscQueryDaily.clicks), 0),
                func.coalesce(func.sum(GscQueryDaily.impressions), 0),
                func.coalesce(
                    func.sum(GscQueryDaily.position * GscQueryDaily.impressions), 0.0
                ),
            )
            .filter(GscQueryDaily.date >= begin, GscQueryDaily.date <= finish)
            .group_by(GscQueryDaily.query)
            .all()
        )
        return {
            q: Metrics(int(c), int(i), float(w)) for q, c, i, w in rows
        }

    with get_session() as session:
        current = aggregate(session, start, end)
        previous = aggregate(session, prev_start, prev_end)
        metrics = {
            m.keyword: m for m in session.query(KeywordMetric).all()
        }

    zero = Metrics()
    rows = []
    for term, now in current.items():
        if search and search.lower() not in term.lower():
            continue
        if now.impressions < 5:
            continue
        market = metrics.get(term)
        was = previous.get(term, zero)
        in_striking = 5.0 <= now.position <= 40.0 and now.impressions >= 20
        if striking_only and not in_striking:
            continue
        # Opportunity: impressions the site already earns, weighted by how
        # much of the click-through it is currently leaving on the table.
        # Deliberately uses the site's own impressions rather than market
        # volume, because volume is missing for most terms until it's paid for
        # — and a ranking built on a mostly-absent column is a ranking of
        # which terms happen to have been looked up.
        headroom = max(0.0, 0.05 - now.ctr)
        rows.append({
            "query": term,
            "current": now,
            "previous": was,
            "clicks_delta": now.clicks - was.clicks,
            "position_delta": (now.position - was.position) if was.impressions else None,
            "volume": market.search_volume if market else None,
            "cpc": market.cpc if market else None,
            "competition": market.competition if market else None,
            "striking": in_striking,
            "opportunity": now.impressions * headroom,
        })

    keys = {
        "opportunity": lambda r: -r["opportunity"],
        "impressions": lambda r: -r["current"].impressions,
        "clicks": lambda r: -r["current"].clicks,
        "position": lambda r: r["current"].position,
        "volume": lambda r: -(r["volume"] or 0),
        "movers": lambda r: -r["clicks_delta"],
    }
    rows.sort(key=keys.get(order, keys["opportunity"]))

    from dashboard.jobs.dataforseo_keywords import (
        COST_PER_REQUEST,
        budget_remaining,
        spend_to_date,
    )

    return {
        "rows": rows[:limit],
        "total": len(rows),
        "window": (start, end),
        "previous_window": (prev_start, prev_end),
        "with_market_data": sum(1 for r in rows if r["volume"] is not None),
        "terms_known": len(metrics),
        "spend": {
            "spent": spend_to_date(),
            "remaining": budget_remaining(),
            "per_request": COST_PER_REQUEST,
        },
    }


def coverage(today: date | None = None) -> dict:
    """What the database actually holds — the honest header of every page."""
    today = today or date.today()
    with get_session() as session:
        site_days = session.query(func.count(GscSiteDaily.date)).scalar() or 0
        page_rows = session.query(func.count(GscPageDaily.id)).scalar() or 0
        latest = session.query(func.max(GscSiteDaily.date)).scalar()
        earliest = session.query(func.min(GscSiteDaily.date)).scalar()
    return {
        "site_days": site_days,
        "page_rows": page_rows,
        "latest_day": latest,
        "earliest_day": earliest,
        "settled_through": settled_through(today),
        "stale_days": (today - latest).days if latest else None,
    }


# ── Competitors ─────────────────────────────────────────────────────


def competitors(*, days: int = 90, today: date | None = None) -> dict:
    """Everything the Competitors page shows, in one read.

    Four questions, because they're the four a flooring retailer can act on:
    what do they sell that we don't, who shows a price where we don't, what
    are they pushing, and what are they writing about.

    The price comparison covers **confirmed matches only**. A proposed match
    is a guess, and a guess that reaches a price table becomes "they're
    undercutting us on X" about a product that isn't X.
    """
    today = today or date.today()
    since = datetime.combine(today - timedelta(days=days), datetime.min.time())

    with get_session() as session:
        sites = (
            session.query(Competitor)
            .order_by(Competitor.enabled.desc(), Competitor.name)
            .all()
        )
        by_id = {c.id: c for c in sites}

        counts = dict(
            session.query(
                CompetitorProduct.competitor_id, func.count(CompetitorProduct.id)
            ).group_by(CompetitorProduct.competitor_id).all()
        )
        priced = dict(
            session.query(
                CompetitorProduct.competitor_id, func.count(CompetitorProduct.id)
            )
            .filter(CompetitorProduct.price_min > 0)
            .group_by(CompetitorProduct.competitor_id)
            .all()
        )
        post_counts = dict(
            session.query(
                CompetitorPost.competitor_id, func.count(CompetitorPost.id)
            )
            .filter(CompetitorPost.published_at >= since)
            .group_by(CompetitorPost.competitor_id)
            .all()
        )

        overview = []
        for site in sites:
            total = counts.get(site.id, 0)
            with_price = priced.get(site.id, 0)
            recent_posts = post_counts.get(site.id, 0)
            overview.append({
                "competitor": site,
                "products": total,
                "priced": with_price,
                # The comparison that matters most for this store: ~94% of our
                # own catalogue hides its price behind "call for price". A
                # competitor showing prices on the same goods is winning the
                # click before anyone picks up a phone.
                "priced_pct": (with_price / total * 100) if total else 0.0,
                "posts_in_window": recent_posts,
                "posts_per_month": round(recent_posts / (days / 30.0), 1)
                if days else 0.0,
            })

        # Our own equivalents, so every competitor number has something to be
        # compared against rather than sitting on its own.
        our_total = session.query(func.count(ShopifyProduct.id)).scalar() or 0
        our_priced = (
            session.query(func.count(ShopifyProduct.id))
            .filter(ShopifyProduct.price_min > 0)
            .scalar()
        ) or 0
        our_posts = (
            session.query(func.count(BlogArticle.id))
            .filter(BlogArticle.published_at >= since)
            .scalar()
        ) or 0

        posts = (
            session.query(CompetitorPost)
            .order_by(
                CompetitorPost.published_at.is_(None).asc(),
                CompetitorPost.published_at.desc(),
            )
            .limit(40)
            .all()
        )

        best = (
            session.query(CompetitorProduct)
            .filter(CompetitorProduct.best_seller_rank.isnot(None))
            .order_by(CompetitorProduct.best_seller_rank.asc())
            .limit(25)
            .all()
        )

        # Confirmed matches, with both prices side by side.
        confirmed = (
            session.query(CompetitorMatch)
            .filter(CompetitorMatch.status == MatchStatus.confirmed.value)
            .all()
        )
        comparisons = _pair_up(session, confirmed, by_id, with_delta=True)
        comparisons.sort(key=lambda c: (c["delta"] is None, -(c["delta"] or 0.0)))

        pending = (
            session.query(CompetitorMatch)
            .filter(CompetitorMatch.status == MatchStatus.proposed.value)
            .order_by(CompetitorMatch.score.desc())
            .limit(30)
            .all()
        )
        queue = _pair_up(session, pending, by_id, with_delta=False)

        # Brands they carry and we don't — the assortment gap, and the
        # cheapest question here to answer well.
        our_vendors = {
            (v or "").strip().lower()
            for (v,) in session.query(ShopifyProduct.vendor).distinct()
            if v
        }
        their_vendors = (
            session.query(
                CompetitorProduct.vendor,
                func.count(CompetitorProduct.id),
                func.min(CompetitorProduct.price_min),
            )
            .filter(CompetitorProduct.vendor.isnot(None))
            .group_by(CompetitorProduct.vendor)
            .order_by(func.count(CompetitorProduct.id).desc())
            .all()
        )
        gaps = [
            {"vendor": v, "products": n, "from_price": lo}
            for v, n, lo in their_vendors
            if (v or "").strip().lower() not in our_vendors
        ][:25]

    return {
        "overview": overview,
        "visibility_gaps": price_visibility_gaps(),
        "ours": {
            "products": our_total,
            "priced": our_priced,
            "priced_pct": (our_priced / our_total * 100) if our_total else 0.0,
            "posts_in_window": our_posts,
            "posts_per_month": round(our_posts / (days / 30.0), 1) if days else 0.0,
        },
        "posts": posts,
        "best_sellers": best,
        "comparisons": comparisons,
        "queue": queue,
        "gaps": gaps,
        "window_days": days,
        "configured": bool(sites),
    }


def _pair_up(session, matches, by_id: dict, *, with_delta: bool) -> list[dict]:
    """Attach both products (and optionally the price gap) to each match.

    Batched rather than per-match lazy loads: the review queue is 30 rows and
    the confirmed list grows without bound, so N+1 here would be the page's
    slowest thing for no reason.
    """
    their_ids = [m.competitor_product_id for m in matches]
    our_ids = [m.shopify_product_id for m in matches]
    theirs_by_id = {
        p.id: p
        for p in session.query(CompetitorProduct).filter(
            CompetitorProduct.id.in_(their_ids)
        )
    } if their_ids else {}
    ours_by_id = {
        p.id: p
        for p in session.query(ShopifyProduct).filter(
            ShopifyProduct.id.in_(our_ids)
        )
    } if our_ids else {}

    out: list[dict] = []
    for match in matches:
        theirs = theirs_by_id.get(match.competitor_product_id)
        ours = ours_by_id.get(match.shopify_product_id)
        if theirs is None or ours is None:
            continue
        row = {
            "match": match,
            "theirs": theirs,
            "ours": ours,
            "competitor": by_id.get(theirs.competitor_id),
        }
        if with_delta:
            # Our price is 0.00 for ~94% of the catalogue — hidden, not free.
            # Reported as "hidden" rather than as a huge win, which is what a
            # naive subtraction would call it.
            hidden = ours.price_min <= 0
            delta = None if hidden else round(ours.price_min - theirs.price_min, 2)
            row["our_price_hidden"] = hidden
            row["delta"] = delta
            row["we_are_higher"] = bool(delta is not None and delta > 0)
        out.append(row)
    return out


def our_terms_in(text: str, *, limit: int = 5, today: date | None = None) -> list[str]:
    """Our own striking-distance search terms that appear in `text`.

    Used when countering a competitor's post: the reply is only worth writing
    on terms Google already associates with this site. Without that filter,
    "counter this" just means writing about whatever they happened to write
    about, which is how a content calendar fills up with articles that rank
    for nothing.

    Matched by whole phrase rather than by word overlap — "laminate" appearing
    in both is not evidence, "laminate stair nosing" is.
    """
    today = today or date.today()
    end = settled_through(today)
    start = end - timedelta(days=27)
    haystack = " " + re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()) + " "
    haystack = re.sub(r"\s+", " ", haystack)

    with get_session() as session:
        rows = (
            session.query(
                GscQueryDaily.query,
                func.sum(GscQueryDaily.impressions).label("impressions"),
                func.sum(GscQueryDaily.position * GscQueryDaily.impressions),
            )
            .filter(GscQueryDaily.date >= start, GscQueryDaily.date <= end)
            .group_by(GscQueryDaily.query)
            .order_by(func.sum(GscQueryDaily.impressions).desc())
            .all()
        )

    out: list[str] = []
    for query, impressions, weight in rows:
        if not query:
            continue
        impressions = int(impressions or 0)
        if impressions < 10:
            continue
        position = float(weight or 0.0) / impressions if impressions else 0.0
        if not (STRIKING_MIN <= position <= STRIKING_MAX):
            continue
        if f" {query.strip().lower()} " in haystack:
            out.append(query.strip())
        if len(out) >= limit:
            break
    return out


# ── The content pipeline ────────────────────────────────────────────

#: What each stage means and what unblocks it. The order is the order an
#: article moves through, which is also the order to read the funnel in.
PIPELINE_STAGES = (
    ("queued", "Queued", "Scheduled, not written yet"),
    ("drafting", "Drafting", "The pipeline is working on it"),
    ("held", "Held by QA", "Written, but QA wants a human read"),
    ("shopify_draft", "Draft in Shopify", "Written and uploaded — needs one click"),
    ("stranded", "Stranded", "Passed QA but never reached Shopify"),
    ("published", "Published", "Live on the site"),
)


def _blocked_reason(article, *, confidence_threshold: float) -> tuple[str, str, str]:
    """Why one written article isn't live, and what would change that.

    Returns `(stage, reason, action)`. Derived from the pipeline's actual
    publish gate (`article_graph.route_after_qa`), not guessed:
    auto-publish requires Shopify to be configured and enabled AND a QA
    verdict of "pass" AND confidence at or above the threshold.

    Note what is deliberately *not* here: the SEO score. It gates one
    revision pass and nothing else, so an article can score 60 and still
    publish. Listing it as a blocking reason would send you to fix the wrong
    thing — which is exactly what the raw numbers on this page invite.
    """
    if (article.failure_reason or "").strip():
        return ("held", f"The run failed: {article.failure_reason.strip()}",
                "Re-run the article, or queue the topic again.")

    report = article.qa_report or {}
    verdict = str(report.get("verdict") or "").lower()
    confidence = article.qa_confidence_score

    if article.shopify_article_id:
        # It exists in Shopify but isn't live: SHOPIFY_PUBLISH_LIVE=false
        # creates confident articles as hidden drafts on purpose. This is the
        # cheapest pile to clear — the writing is done and reviewed.
        return (
            "shopify_draft",
            "Uploaded to Shopify as a hidden draft, waiting to be published.",
            "Open it in Shopify admin and click Publish.",
        )

    problems = []
    if verdict and verdict != "pass":
        problems.append(f"QA verdict was '{verdict}', not 'pass'")
    if confidence is not None and confidence < confidence_threshold:
        problems.append(
            f"QA confidence {confidence:.2f} is below the {confidence_threshold:g} "
            "threshold"
        )
    if problems:
        return (
            "held",
            " and ".join(problems) + ".",
            "Read it in Linear and publish by hand, or fix and re-run it.",
        )

    return (
        "stranded",
        "Passed QA but no Shopify article exists — the publish step didn't "
        "run or didn't succeed.",
        "Check the article's Linear issue for a publish error, then publish "
        "by hand.",
    )


def content_pipeline(*, limit: int = 60, today: date | None = None) -> dict:
    """The calendar and the backlog: what's coming, and what's stuck where.

    Reads the **pipeline's** own tables directly rather than the dashboard's
    `blog_article` mirror. The mirror carries what's needed to join articles
    to search metrics; this needs the queue, the QA verdicts and the failure
    reasons, which only the pipeline has and which no sync would be right to
    duplicate.
    """
    today = today or date.today()
    try:
        from blog_pipeline.config import get_settings as pipeline_settings
        from blog_pipeline.db.models import (
            Article,
            ArticleStatus,
            CalendarEntry,
            EntryStatus,
        )
        from blog_pipeline.db.session import get_session as pipeline_session
    except Exception as e:  # noqa: BLE001 - reported, not raised
        return {"available": False, "error": f"{type(e).__name__}: {e}"}

    settings = pipeline_settings()
    threshold = settings.confidence_threshold

    try:
        with pipeline_session() as session:
            upcoming = (
                session.query(CalendarEntry)
                .filter(CalendarEntry.status.in_(
                    [EntryStatus.queued, EntryStatus.drafting]
                ))
                .order_by(CalendarEntry.scheduled_date.asc())
                .limit(limit)
                .all()
            )
            unpublished = (
                session.query(Article)
                .filter(Article.status != ArticleStatus.published)
                .order_by(Article.updated_at.desc(), Article.id.desc())
                .limit(limit)
                .all()
            )
            published_count = (
                session.query(func.count(Article.id))
                .filter(Article.status == ArticleStatus.published)
                .scalar()
            ) or 0
            for row in list(upcoming) + list(unpublished):
                session.expunge(row)
    except Exception as e:  # noqa: BLE001 - an unreadable pipeline DB is a
        # message, not a 500. Same handling as the Blog page's own reads.
        return {"available": False, "error": f"{type(e).__name__}: {e}"}

    blocked = []
    for article in unpublished:
        stage, reason, action = _blocked_reason(
            article, confidence_threshold=threshold
        )
        blocked.append({
            "article": article,
            "stage": stage,
            "reason": reason,
            "action": action,
            "days_waiting": (
                (today - article.updated_at.date()).days
                if article.updated_at else None
            ),
        })

    counts = {key: 0 for key, _, _ in PIPELINE_STAGES}
    for entry in upcoming:
        key = "drafting" if entry.status == EntryStatus.drafting else "queued"
        counts[key] += 1
    for row in blocked:
        counts[row["stage"]] += 1
    counts["published"] = published_count

    # Ordered so the cheapest wins come first: a Shopify draft is one click,
    # a QA hold needs reading, a stranded article needs diagnosing.
    order = {"shopify_draft": 0, "held": 1, "stranded": 2}
    blocked.sort(key=lambda r: (order.get(r["stage"], 9), -(r["days_waiting"] or 0)))

    return {
        "available": True,
        "error": None,
        "upcoming": upcoming,
        "blocked": blocked,
        "counts": counts,
        "stages": PIPELINE_STAGES,
        "confidence_threshold": threshold,
        "publishes_live": bool(
            settings.can_autopublish and settings.shopify_publish_live
        ),
        "can_autopublish": bool(settings.can_autopublish),
        "next_due": upcoming[0].scheduled_date if upcoming else None,
        "overdue": [
            e for e in upcoming
            if e.scheduled_date and e.scheduled_date < today
        ],
        "today": today,
    }


def competitor_price_history(
    competitor_product_id: int, *, days: int = 90, today: date | None = None
) -> dict:
    """One matched product's price over time, theirs and ours.

    Ours comes from `shopify_product.price_min` as it stands today, repeated
    across the window rather than read from history — the catalogue snapshot
    overwrites in place and keeps no per-day price of its own. Stated here
    because a flat line that looks like "we never moved" would otherwise be
    read as measured, and it isn't: it's the only value we have.
    """
    today = today or date.today()
    since = today - timedelta(days=days)

    with get_session() as session:
        theirs_row = session.get(CompetitorProduct, competitor_product_id)
        if theirs_row is None:
            return {"found": False}

        snapshots = (
            session.query(CompetitorProductPrice)
            .filter(
                CompetitorProductPrice.competitor_product_id
                == competitor_product_id,
                CompetitorProductPrice.date >= since,
            )
            .order_by(CompetitorProductPrice.date.asc())
            .all()
        )
        match = (
            session.query(CompetitorMatch)
            .filter(
                CompetitorMatch.competitor_product_id == competitor_product_id,
                CompetitorMatch.status == MatchStatus.confirmed.value,
            )
            .first()
        )
        ours_row = (
            session.get(ShopifyProduct, match.shopify_product_id)
            if match else None
        )
        site = session.get(Competitor, theirs_row.competitor_id)

        theirs = [(s.date, s.price_min) for s in snapshots if s.price_min > 0]
        our_price = ours_row.price_min if ours_row else 0.0
        ours = (
            [(day, our_price) for day, _ in theirs] if our_price > 0 else []
        )

        ranks = [
            (s.date, s.best_seller_rank)
            for s in snapshots
            if s.best_seller_rank is not None
        ]

        return {
            "found": True,
            "theirs": theirs,
            "ours": ours,
            "their_product": theirs_row,
            "our_product": ours_row,
            "competitor": site,
            "our_price_is_current_only": bool(ours),
            "ranks": ranks,
            "days": days,
        }


#: Two-word phrases are what identify a flooring range — "euro style",
#: "la foret", "dark river". Single words are hopeless here: "oak", "grey"
#: and "plank" appear in thousands of products across every catalogue and
#: would group unrelated things together with total confidence.
_PHRASE_STOP = frozenset(
    """
    the and for with in of a an by to
    flooring floor floors plank planks board boards tile tiles
    collection series colour color inch mm sqft sq ft
    """.split()
)
_PHRASE_WORD = re.compile(r"[a-z][a-z0-9]{2,}")


def _phrases(title: str) -> set[str]:
    words = [w for w in _PHRASE_WORD.findall((title or "").lower())
             if w not in _PHRASE_STOP]
    return {f"{a} {b}" for a, b in zip(words, words[1:])}


def price_visibility_gaps(*, limit: int = 12) -> list[dict]:
    """Ranges we stock but hide the price of, that a competitor prices openly.

    This is the sharpest thing the competitor data can say, and nothing else
    on the page says it. The match queue can't: it only considers products
    whose price we show, and the whole point here is the ones we don't. So
    the overlap that matters most is invisible everywhere else — it looks
    like "no matches found" rather than "you and they sell the same range
    and only they show a number".

    Grouped by two-word phrase from the product titles rather than by vendor,
    because our vendor field says "D & R Flooring" on 2,702 of 2,796 products
    — it identifies the shop, not the range.
    """
    with get_session() as session:
        ours_hidden = session.query(
            ShopifyProduct.title, ShopifyProduct.price_min
        ).all()
        theirs = (
            session.query(
                CompetitorProduct.title,
                CompetitorProduct.price_min,
                CompetitorProduct.competitor_id,
            )
            .filter(CompetitorProduct.price_min > 0)
            .all()
        )
        names = {c.id: c.name for c in session.query(Competitor)}

    hidden_by_phrase: dict[str, int] = {}
    shown_by_phrase: dict[str, int] = {}
    for title, price in ours_hidden:
        target = hidden_by_phrase if (price or 0) <= 0 else shown_by_phrase
        for phrase in _phrases(title):
            target[phrase] = target.get(phrase, 0) + 1
    if not hidden_by_phrase:
        return []

    theirs_by_phrase: dict[str, list] = {}
    for title, price, competitor_id in theirs:
        for phrase in _phrases(title):
            if phrase in hidden_by_phrase:
                theirs_by_phrase.setdefault(phrase, []).append(
                    (price, competitor_id)
                )

    rows = []
    for phrase, entries in theirs_by_phrase.items():
        hidden = hidden_by_phrase.get(phrase, 0)
        # A single product on either side is a coincidence, not a range.
        if hidden < 2 or len(entries) < 2:
            continue
        prices = [p for p, _ in entries]
        who = sorted({names.get(cid, "?") for _, cid in entries})
        rows.append({
            "phrase": phrase,
            "ours_hidden": hidden,
            "ours_priced": shown_by_phrase.get(phrase, 0),
            "theirs_priced": len(entries),
            "their_low": min(prices),
            "their_high": max(prices),
            "competitors": who,
        })

    # Most of our hidden products first — that's the size of the exposure.
    rows.sort(key=lambda r: (-r["ours_hidden"], -r["theirs_priced"]))

    # Sub-phrases of a phrase already shown add nothing ("style german" under
    # "euro style"), so keep the first and drop anything sharing a word with
    # an already-kept phrase.
    kept: list[dict] = []
    used: set[str] = set()
    for row in rows:
        words = set(row["phrase"].split())
        if words & used:
            continue
        used |= words
        kept.append(row)
        if len(kept) >= limit:
            break
    return kept
