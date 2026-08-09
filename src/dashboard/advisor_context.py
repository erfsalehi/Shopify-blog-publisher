"""Building the factual brief each advisor scope is given.

Every number here comes from a query against this database. The model gets no
tools and no network, so this file is the complete universe of figures it can
work from — which is the point. It is also stored verbatim on the note, so any
suggestion can be checked against exactly what informed it.

Two habits worth keeping when adding a scope:

**Say what's missing.** "No experiments have been created" is more useful to
the model than an empty section, which it will try to interpret. Absent data
should be stated, not implied.

**Include the caveats the UI shows.** If the Ads page tells the owner not to
trust a percentage because the tracking tags are younger than the comparison
window, the model needs to know that too — otherwise it confidently explains a
382% rise that is an artefact.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func

from dashboard import reporting
from dashboard.db import get_session
from dashboard.jobs.gsc import settled_through
from dashboard.models import (
    AdsCampaignDaily,
    Alert,
    Experiment,
    ExperimentProduct,
    Ga4EventDaily,
    GscQueryDaily,
    JobRun,
    JobStatus,
    KeywordMetric,
    ShopifyProduct,
)


def _table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return "(none)"
    out = [" | ".join(headers), "-" * 40]
    for row in rows:
        out.append(" | ".join("" if c is None else str(c) for c in row))
    return "\n".join(out)


def _health() -> str:
    """Shared preamble: is the data even trustworthy right now?

    On every scope, because advice built on a sync that failed four days ago
    is worse than no advice, and only this section can tell the model that.
    """
    coverage = reporting.coverage()
    with get_session() as session:
        runs = session.query(JobRun).order_by(
            JobRun.started_at.desc(), JobRun.id.desc()
        ).all()
        latest: dict[str, JobRun] = {}
        for run in runs:
            latest.setdefault(run.job, run)
        failing = [n for n, r in latest.items() if r.status == JobStatus.error.value]
        open_alerts = session.query(Alert).filter(
            Alert.acknowledged_at.is_(None), Alert.resolved_at.is_(None)
        ).count()

    latest, settled = coverage["latest_day"], coverage["settled_through"]
    lines = [
        "DATA HEALTH",
        f"- Search Console data goes up to {latest}. Google restates its most "
        f"recent 3 days, so every comparison below ends at {settled} and never "
        "includes unsettled days.",
        f"- Open alerts: {open_alerts}",
    ]
    # Two different problems, and the model should distinguish them: data that
    # is behind because a sync hasn't run, versus data that is complete.
    if latest and latest < settled:
        behind = (settled - latest).days
        lines.append(
            f"- WARNING: the sync is {behind} day(s) behind — settled data "
            f"exists up to {settled} but has not been pulled. Recent figures "
            "may understate reality."
        )
    if failing:
        lines.append(
            f"- WARNING: these syncs last failed: {', '.join(failing)}. "
            "Figures below may be stale."
        )
    return "\n".join(lines)


def _overview(today: date) -> str:
    summary = reporting.site_summary(window_days=28, today=today)
    pages = reporting.top_pages(window_days=28, limit=12, today=today)
    if not summary["has_data"]:
        return "No Search Console data has been synced yet."

    cur, prev = summary["current"], summary["previous"]
    lines = [
        f"ORGANIC SEARCH, {summary['current_window'][0]} to "
        f"{summary['current_window'][1]} (28 days), versus the previous 28 days "
        f"({summary['previous_window'][0]} to {summary['previous_window'][1]}):",
        f"- Clicks: {cur.clicks:,} (was {prev.clicks:,})",
        f"- Impressions: {cur.impressions:,} (was {prev.impressions:,})",
        f"- CTR: {cur.ctr * 100:.2f}% (was {prev.ctr * 100:.2f}%)",
        f"- Average position: {cur.position:.1f} (was {prev.position:.1f}); "
        "lower is better, and both are impression-weighted as Google's are.",
        "",
        "TOP PAGES THIS WINDOW (clicks | change | impressions | CTR | position):",
        _table(
            ["page", "clicks", "change", "impressions", "ctr", "position"],
            [
                [
                    row["page"].replace("https://drflooring.ca", ""),
                    row["current"].clicks,
                    f"{row['clicks_delta']:+d}",
                    row["current"].impressions,
                    f"{row['current'].ctr * 100:.2f}%",
                    f"{row['current'].position:.1f}",
                ]
                for row in pages
            ],
        ),
    ]
    return "\n".join(lines)


def _products(today: date) -> str:
    data = reporting.products(window_days=28, order="clicks", limit=15, today=today)
    with get_session() as session:
        total = session.query(ShopifyProduct).count()
        priced = session.query(ShopifyProduct).filter(
            ShopifyProduct.price_min > 0
        ).count()
    lines = [
        f"CATALOGUE: {total:,} products, of which {priced} show a real price. "
        "The rest are hidden behind the Orichi 'Call for price' app, which is "
        "deliberate — the conversion is a phone call.",
        f"{data['with_traffic']:,} products had search impressions in the last "
        "28 days.",
        "",
        "BEST PERFORMING PRODUCTS:",
        _table(
            ["product", "price", "clicks", "change", "impressions", "ctr", "position"],
            [
                [
                    r["product"].title[:50],
                    f"${r['product'].price_min:.2f}" if r["product"].price_min else "call",
                    r["current"].clicks,
                    f"{r['clicks_delta']:+d}",
                    r["current"].impressions,
                    f"{r['current'].ctr * 100:.2f}%",
                    f"{r['current'].position:.1f}" if r["current"].impressions else "-",
                ]
                for r in data["rows"]
            ],
        ),
    ]
    fallers = reporting.products(
        window_days=28, order="fallers", limit=8, today=today
    )["rows"]
    losing = [r for r in fallers if r["clicks_delta"] < 0]
    if losing:
        lines += [
            "",
            "PRODUCTS LOSING THE MOST CLICKS:",
            _table(
                ["product", "clicks now", "change", "impressions"],
                [[r["product"].title[:50], r["current"].clicks,
                  f"{r['clicks_delta']:+d}", r["current"].impressions]
                 for r in losing],
            ),
        ]
    return "\n".join(lines)


def _blog(today: date) -> str:
    data = reporting.blog_posts(window_days=28, order="decay", today=today)
    if not data["total"]:
        return "No blog articles have been indexed yet."
    lines = [
        f"BLOG: {data['total']} articles, {data['live']} live on Shopify. "
        f"{data['decaying']} are losing impressions against the previous 28 "
        f"days. {data['ever_refreshed']} have ever been rewritten by the "
        "refresh pipeline.",
        "",
        "Decay is ranked by ABSOLUTE impressions lost, not percentage — a post "
        "falling 25 to 1 is a 96% collapse worth 24 impressions, while 18,272 "
        "to 5,497 is only -70% and worth 12,775.",
        "",
        "WORST DECAY:",
        _table(
            ["article", "impressions", "lost", "clicks", "position", "last refreshed"],
            [
                [
                    r["article"].title[:52],
                    r["current"].impressions,
                    r["impressions_lost"],
                    r["current"].clicks,
                    f"{r['current'].position:.1f}" if r["current"].impressions else "-",
                    r["article"].last_refreshed_at.date()
                    if r["article"].last_refreshed_at else "never",
                ]
                for r in data["rows"][:12]
            ],
        ),
    ]
    return "\n".join(lines)


def _keywords(today: date) -> str:
    end = settled_through(today)
    start = end - timedelta(days=27)
    location = None
    with get_session() as session:
        rows = (
            session.query(
                GscQueryDaily.query,
                func.sum(GscQueryDaily.clicks),
                func.sum(GscQueryDaily.impressions),
                func.sum(GscQueryDaily.position * GscQueryDaily.impressions),
            )
            .filter(GscQueryDaily.date >= start, GscQueryDaily.date <= end)
            .group_by(GscQueryDaily.query)
            .order_by(func.sum(GscQueryDaily.impressions).desc())
            .limit(400)
            .all()
        )
        metrics = {
            m.keyword: m for m in session.query(KeywordMetric).all()
        }
        if metrics:
            location = next(iter(metrics.values())).location_code

    if not rows:
        return (
            "No Search Console query data yet. Run the Search Console sync — "
            "it now pulls the search terms the site is shown for, not just "
            "pages."
        )

    striking = []
    for query, clicks, impressions, weight in rows:
        impressions = int(impressions or 0)
        if impressions < 20:
            continue
        position = float(weight or 0) / impressions if impressions else 0
        if not (5 <= position <= 40):
            continue
        m = metrics.get(query)
        striking.append([
            query[:48], int(clicks or 0), impressions, f"{position:.1f}",
            m.search_volume if m else "?",
            f"${m.cpc:.2f}" if m and m.cpc else "?",
        ])

    lines = [
        f"SEARCH TERMS, {start} to {end} (28 days).",
        "",
        "STRIKING DISTANCE — terms with real impressions at positions 5-40. "
        "Google already considers the site relevant to these, so they are a "
        "far shorter path to page one than a term with no history. "
        "'volume' and 'cpc' come from DataForSEO; '?' means it has not been "
        "looked up (each lookup costs money and is budget-capped).",
        _table(
            ["term", "clicks", "impressions", "position", "volume/mo", "cpc"],
            striking[:30],
        ),
    ]
    if metrics:
        lines.append("")
        lines.append(
            f"{len(metrics)} terms have market data, for location code "
            f"{location} (2124 = Canada, 2840 = United States)."
        )
    else:
        lines.append("")
        lines.append(
            "No DataForSEO market data yet — run the keyword market data job."
        )
    return "\n".join(lines)


def _ads(today: date) -> str:
    ads = reporting.ads_overview(window_days=28, today=today)
    if not ads["has_data"]:
        return "No Google Ads data has been synced yet."
    lines = [
        f"GOOGLE ADS, {ads['window'][0]} to {ads['window'][1]} (28 days):",
        f"- Spend: ${ads['spend']:,.2f}",
        f"- Paid clicks: {ads['clicks']:,}"
        + (f" at ${ads['cpc']:.2f} each" if ads["cpc"] else ""),
        f"- Ads-reported conversions: {ads['conversions']:.1f}"
        + (f" at ${ads['cost_per_conversion']:.2f} each"
           if ads["cost_per_conversion"] else ""),
        f"- Organic clicks over the same dates: {ads['organic_clicks']:,} "
        "(these cost nothing per click)",
        f"- GA4 call/WhatsApp/directions events: {ads['calls']}",
        "",
        "IMPORTANT: these are totals over the same dates, not a funnel. "
        "Nothing here attributes a specific call to a specific campaign.",
    ]
    if ads["calls_baseline_incomplete"]:
        lines.append(
            f"IMPORTANT: the GA4 conversion tags only start producing data on "
            f"{ads['first_event_day']}, which is after the comparison window "
            "begins. Any percentage change on call events measures when "
            "tracking was installed, not business performance. Do not comment "
            "on it."
        )
    lines += [
        "",
        "BY CAMPAIGN:",
        _table(
            ["campaign", "type", "spend", "clicks", "cpc", "conversions", "cost/conv"],
            [
                [
                    c["campaign"][:36], (c["campaign_type"] or "")[:16],
                    f"${c['spend']:,.2f}", c["clicks"],
                    f"${c['cpc']:.2f}" if c["cpc"] else "-",
                    f"{c['conversions']:.1f}",
                    f"${c['cost_per_conversion']:.2f}"
                    if c["cost_per_conversion"] else "none",
                ]
                for c in ads["campaigns"]
            ],
        ),
    ]

    end = settled_through(today)
    with get_session() as session:
        events = session.query(
            Ga4EventDaily.event_name, func.sum(Ga4EventDaily.event_count)
        ).filter(
            Ga4EventDaily.date >= end - timedelta(days=27),
            Ga4EventDaily.date <= end,
        ).group_by(Ga4EventDaily.event_name).all()
    if events:
        lines += ["", "CONVERSION EVENTS (28 days):"]
        lines += [f"- {name}: {int(total)}" for name, total in events]
    return "\n".join(lines)


def _experiments(today: date) -> str:
    from dashboard import experiments as exp

    with get_session() as session:
        rows = session.query(Experiment).order_by(Experiment.id.desc()).all()
        for row in rows:
            session.expunge(row)
    if not rows:
        return (
            "No experiments have been created. The framework supports cohort "
            "and time based tests scored by difference-in-differences; "
            "per-visitor A/B testing is impossible here because search shows "
            "one title to everyone."
        )
    out = []
    for experiment in rows:
        result = exp.score(experiment.id, today=today)
        with get_session() as session:
            counts = {
                cohort: session.query(ExperimentProduct).filter(
                    ExperimentProduct.experiment_id == experiment.id,
                    ExperimentProduct.cohort == cohort,
                ).count()
                for cohort in ("treatment", "control")
            }
        block = [
            f"EXPERIMENT '{experiment.name}' ({experiment.variable}, "
            f"{experiment.status}), treatment {counts['treatment']} products, "
            f"control {counts['control']}.",
        ]
        if experiment.hypothesis:
            block.append(f"Hypothesis: {experiment.hypothesis}")
        if result.get("scorable"):
            block += [
                f"Treatment changed {result['treatment_delta']:+.2f}, control "
                f"{result['control_delta']:+.2f}, difference "
                f"{result['difference']:+.2f} "
                f"({'CTR percentage points' if result['metric'] == 'ctr' else 'clicks'} "
                "per product).",
                f"Permutation test p = {result['p_value']:.3f} over "
                f"{result['days_running']} days.",
                f"Verdict: {result['verdict']}",
            ]
        else:
            block.append(f"Not scorable: {result.get('reason')}")
        out.append("\n".join(block))
    return "\n\n".join(out)


def _competitors(today: date) -> str:
    data = reporting.competitors(days=90, today=today)
    if not data["configured"]:
        return (
            "No competitors are being watched yet. Nothing can be said about "
            "the competitive position until some are added on the Settings "
            "page."
        )

    ours = data["ours"]
    lines = [
        "COMPETITORS (their public sites, last 90 days):",
        f"- We list {ours['products']:,} products, "
        f"{ours['priced']:,} of them with a visible price "
        f"({ours['priced_pct']:.0f}%). The rest say 'call for price'.",
        f"- We published {ours['posts_in_window']} articles "
        f"({ours['posts_per_month']}/month).",
        "",
    ]
    for row in data["overview"]:
        site = row["competitor"]
        lines.append(
            f"- {site.name}: {row['products']:,} products, "
            f"{row['priced']:,} priced ({row['priced_pct']:.0f}%), "
            f"{row['posts_per_month']} posts/month."
            + (f" Not readable: {site.last_error}" if site.last_error else "")
        )

    if data["comparisons"]:
        lines += ["", "CONFIRMED PRODUCT MATCHES (owner-verified pairs):"]
        for row in data["comparisons"][:15]:
            theirs = row["theirs"]
            if row["our_price_hidden"]:
                lines.append(
                    f"- {row['ours'].title}: our price is hidden; "
                    f"{row['competitor'].name if row['competitor'] else 'they'} "
                    f"show ${theirs.price_min:.2f}."
                )
            else:
                lines.append(
                    f"- {row['ours'].title}: ours ${row['ours'].price_min:.2f} "
                    f"vs theirs ${theirs.price_min:.2f} "
                    f"({row['delta']:+.2f})."
                )
    else:
        lines += [
            "",
            "No product matches have been confirmed yet, so no price "
            "comparison exists. Do not infer one from the catalogue sizes.",
        ]

    if data["best_sellers"]:
        lines += ["", "WHAT SELLS FOR THEM (their own best-selling order):"]
        for product in data["best_sellers"][:10]:
            price = (
                f"${product.price_min:.2f}" if product.price_min > 0 else "price hidden"
            )
            lines.append(f"- #{product.best_seller_rank} {product.title} ({price})")

    if data["gaps"]:
        brands = ", ".join(
            f"{g['vendor']} ({g['products']})" for g in data["gaps"][:10]
        )
        lines += ["", f"BRANDS THEY CARRY AND WE DO NOT: {brands}."]

    recent = [p for p in data["posts"] if p.published_at][:12]
    if recent:
        lines += ["", "THEIR RECENT POSTS:"]
        for post in recent:
            lines.append(
                f"- {post.published_at:%Y-%m-%d}: {post.title}"
                + (" [already countered]" if post.countered_at else "")
            )

    lines += [
        "",
        "IMPORTANT: a best-selling ORDER is not sales volume — it ranks their "
        "own products against each other and says nothing about how many they "
        "sell or how that compares to us. No traffic, revenue or conversion "
        "data exists for any competitor and none can be inferred here.",
    ]
    return "\n".join(lines)


_BUILDERS = {
    "overview": _overview,
    "products": _products,
    "blog": _blog,
    "keywords": _keywords,
    "ads": _ads,
    "experiments": _experiments,
    "competitors": _competitors,
}


def build_context(scope: str, today: date | None = None) -> str:
    today = today or date.today()
    builder = _BUILDERS[scope]
    return f"{_health()}\n\n{builder(today)}"
