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

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func

from dashboard.db import get_session
from dashboard.jobs.gsc import settled_through
from dashboard.models import (
    AdsCampaignDaily,
    BlogArticle,
    Ga4EventDaily,
    GscPageDaily,
    GscQueryDaily,
    GscSiteDaily,
    KeywordMetric,
    Experiment,
    ExperimentProduct,
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
