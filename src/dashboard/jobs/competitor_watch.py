"""Competitor sites → `competitor_product`, `competitor_post`.

Four jobs, one per thing worth knowing about a competitor: what they sell,
what they write, what's actually selling, and which of their products is
which of ours. Split rather than combined because they want different
cadences (a catalogue changes daily, a blog weekly, a best-seller ranking
barely at all) and because one site refusing to serve its blog shouldn't
cost you its prices.

**One competitor per run, rotating.** Every job picks the competitor whose
data is stalest and does only that one. A Vercel function has 60 seconds;
paging a 3,000-product catalogue politely does not fit alongside four more
of them. Rotation means N competitors are covered within N days with no run
near the ceiling, and a slow site delays only itself.

Two structural rules, both learned the hard way against this machine's
flaky proxy:

  * **The attempt is stamped before the fetch, in its own transaction.**
    Otherwise a site that always fails keeps its timestamp null, sorts first
    forever, and starves every other competitor of its turn.
  * **No database session is held open across the network.** A catalogue
    crawl is a dozen requests over several seconds; holding a Postgres
    connection through that wastes the one connection a serverless
    invocation gets, and on Neon it's a real socket sitting idle.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from sqlalchemy import func

from dashboard import competitors as fetchers
from dashboard.competitors import FetchError
from dashboard.db import get_session
from dashboard.jobs.registry import JobResult, JobSpec, register
from dashboard.models import (
    Competitor,
    CompetitorPlatform,
    CompetitorPost,
    CompetitorProduct,
    CompetitorProductPrice,
)

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _no_competitors() -> JobResult:
    return JobResult(
        skipped=True,
        skip_reason=(
            "No competitors configured. Add them on the Settings page — "
            "a name and a site URL is all that's needed."
        ),
    )


def _claim_stalest(order_column=None) -> tuple[int, str, str] | None:
    """Pick the competitor due next and stamp the attempt immediately.

    Returns `(id, name, base_url)`, or None if there are none enabled. The
    stamp is committed before this returns, so a fetch that then dies takes
    the competitor out of pole position anyway and the next run moves on.
    """
    with get_session() as session:
        column = order_column if order_column is not None else Competitor.last_checked_at
        competitor = (
            session.query(Competitor)
            .filter(Competitor.enabled.is_(True))
            .order_by(column.is_(None).desc(), column.asc())
            .first()
        )
        if competitor is None:
            return None
        competitor.last_checked_at = _utcnow()
        return competitor.id, competitor.name, competitor.base_url


def _record_failure(competitor_id: int, message: str) -> None:
    with get_session() as session:
        row = session.get(Competitor, competitor_id)
        if row is not None:
            row.last_error = message[:2000]


def _set_platform(competitor_id: int, platform: str) -> None:
    with get_session() as session:
        row = session.get(Competitor, competitor_id)
        if row is not None:
            row.platform = platform


def _platform_of(competitor_id: int) -> str:
    with get_session() as session:
        row = session.get(Competitor, competitor_id)
        return row.platform if row else CompetitorPlatform.unknown.value


# ── Catalogue ────────────────────────────────────────────────────────


def sync_competitor_catalog(today: date | None = None) -> JobResult:
    today = today or date.today()
    claimed = _claim_stalest()
    if claimed is None:
        return _no_competitors()
    cid, name, base_url = claimed

    # ── network, with no session open ────────────────────────────────
    try:
        base = fetchers.normalize_base(base_url)
        platform = _platform_of(cid)
        if platform != CompetitorPlatform.shopify.value:
            platform = fetchers.probe_platform(base)
            _set_platform(cid, platform)
        if platform != CompetitorPlatform.shopify.value:
            reason = (
                "Not a Shopify storefront — nothing machine-readable at "
                "/products.json. Prices for this one would need per-page "
                "scraping, which isn't built."
            )
            _record_failure(cid, reason)
            return JobResult(
                skipped=True,
                skip_reason=f"{name}: {reason}",
                detail={"competitor": name, "platform": platform},
            )
        fetched = fetchers.fetch_shopify_catalog(base)
    except FetchError as e:
        _record_failure(cid, str(e))
        return JobResult(
            skipped=True, skip_reason=f"{name}: {e}", detail={"competitor": name}
        )

    # ── write ────────────────────────────────────────────────────────
    now = _utcnow()
    with get_session() as session:
        existing = {
            row.handle: row
            for row in session.query(CompetitorProduct).filter(
                CompetitorProduct.competitor_id == cid
            )
        }
        created = updated = priced = 0
        for raw in fetched.products:
            row = existing.get(raw.handle)
            if row is None:
                row = CompetitorProduct(
                    competitor_id=cid, handle=raw.handle, first_seen=now
                )
                session.add(row)
                existing[raw.handle] = row
                created += 1
            else:
                updated += 1
            row.external_id = raw.external_id
            row.title = raw.title
            row.vendor = raw.vendor
            row.product_type = raw.product_type
            row.url = raw.url
            row.price_min = raw.price_min
            row.price_max = raw.price_max
            row.currency = raw.currency
            row.available = raw.available
            row.published_at = raw.published_at
            row.last_seen = now
            if raw.price_min > 0:
                priced += 1

        session.flush()  # ids for the snapshot rows below

        # One price row per product per day, upserted — the snapshot shape
        # every other table here uses. Without it, "are they discounting?"
        # can only ever be answered about today.
        seen_handles = {r.handle for r in fetched.products}
        ids = [row.id for h, row in existing.items() if h in seen_handles]
        todays = {
            p.competitor_product_id: p
            for p in session.query(CompetitorProductPrice).filter(
                CompetitorProductPrice.date == today,
                CompetitorProductPrice.competitor_product_id.in_(ids),
            )
        } if ids else {}

        for raw in fetched.products:
            row = existing.get(raw.handle)
            if row is None:
                continue
            snap = todays.get(row.id)
            if snap is None:
                snap = CompetitorProductPrice(competitor_product_id=row.id, date=today)
                session.add(snap)
            snap.price_min = raw.price_min
            snap.price_max = raw.price_max
            snap.available = raw.available

        gone = sum(1 for r in existing.values() if r.last_seen < now)

        competitor = session.get(Competitor, cid)
        if competitor is not None:
            competitor.last_error = None

    return JobResult(
        rows=created + updated,
        detail={
            "competitor": name,
            "pages": fetched.pages,
            "new_products": created,
            "updated_products": updated,
            "with_visible_price": priced,
            "no_longer_listed": gone,
        },
    )


# ── Blog ─────────────────────────────────────────────────────────────


def sync_competitor_posts() -> JobResult:
    with get_session() as session:
        # Ordered by the newest post we've recorded rather than a separate
        # timestamp: a competitor with no posts sorts first, which is the one
        # worth looking at.
        row = (
            session.query(
                Competitor.id,
                Competitor.name,
                Competitor.base_url,
                func.max(CompetitorPost.first_seen).label("seen"),
            )
            .filter(Competitor.enabled.is_(True))
            .outerjoin(CompetitorPost, CompetitorPost.competitor_id == Competitor.id)
            .group_by(Competitor.id, Competitor.name, Competitor.base_url)
            .order_by(func.max(CompetitorPost.first_seen).is_(None).desc(),
                      func.max(CompetitorPost.first_seen).asc())
            .first()
        )
        if row is None:
            return _no_competitors()
        cid, name, base_url = row.id, row.name, row.base_url

    try:
        fetched = fetchers.fetch_posts(fetchers.normalize_base(base_url))
    except FetchError as e:
        _record_failure(cid, str(e))
        return JobResult(
            skipped=True, skip_reason=f"{name}: {e}", detail={"competitor": name}
        )

    now = _utcnow()
    with get_session() as session:
        known = {
            url
            for (url,) in session.query(CompetitorPost.url).filter(
                CompetitorPost.competitor_id == cid
            )
        }
        created = 0
        newest = None
        for raw in fetched.posts:
            if raw.url in known:
                continue
            session.add(
                CompetitorPost(
                    competitor_id=cid,
                    url=raw.url,
                    title=raw.title,
                    summary=raw.summary,
                    author=raw.author,
                    published_at=raw.published_at,
                    first_seen=now,
                )
            )
            known.add(raw.url)
            created += 1
            if raw.published_at and (newest is None or raw.published_at > newest):
                newest = raw.published_at

    return JobResult(
        rows=created,
        detail={
            "competitor": name,
            "in_feed": len(fetched.posts),
            "new_posts": created,
            "newest": newest.isoformat(sep=" ", timespec="minutes") if newest else None,
        },
    )


# ── Best sellers ─────────────────────────────────────────────────────


def sync_competitor_bestsellers(today: date | None = None) -> JobResult:
    """Their sales ranking, the nearest public thing to their revenue.

    See `competitors.fetch_shopify_bestsellers` for why this reads HTML and
    not the JSON endpoint.
    """
    today = today or date.today()

    with get_session() as session:
        row = (
            session.query(
                Competitor.id,
                Competitor.name,
                Competitor.base_url,
            )
            .filter(
                Competitor.enabled.is_(True),
                Competitor.platform == CompetitorPlatform.shopify.value,
            )
            .outerjoin(
                CompetitorProduct, CompetitorProduct.competitor_id == Competitor.id
            )
            .group_by(Competitor.id, Competitor.name, Competitor.base_url)
            .order_by(
                func.max(CompetitorProduct.best_seller_at).is_(None).desc(),
                func.max(CompetitorProduct.best_seller_at).asc(),
            )
            .first()
        )
        if row is None:
            return JobResult(
                skipped=True,
                skip_reason=(
                    "No Shopify competitors to rank yet. Run the competitor "
                    "catalogue sync first — that's what detects the platform."
                ),
            )
        cid, name, base_url = row.id, row.name, row.base_url

    try:
        ranked = fetchers.fetch_shopify_bestsellers(
            fetchers.normalize_base(base_url)
        )
    except FetchError as e:
        _record_failure(cid, str(e))
        return JobResult(
            skipped=True, skip_reason=f"{name}: {e}", detail={"competitor": name}
        )

    now = _utcnow()
    with get_session() as session:
        rows = {
            row.handle: row
            for row in session.query(CompetitorProduct).filter(
                CompetitorProduct.competitor_id == cid
            )
        }
        ranked_rows = unknown = 0
        for position, handle in enumerate(ranked, start=1):
            row = rows.get(handle)
            if row is None:
                # In their best-seller list but not in our copy of their
                # catalogue — the catalogue sync hasn't caught up. Counted so
                # the run says so rather than looking complete.
                unknown += 1
                continue
            row.best_seller_rank = position
            row.best_seller_at = now
            ranked_rows += 1

        # Rank belongs on the daily snapshot too — "they pushed this from #40
        # to #3" is the interesting shape, and it needs history.
        ids = [r.id for r in rows.values() if r.best_seller_rank]
        todays = {
            p.competitor_product_id: p
            for p in session.query(CompetitorProductPrice).filter(
                CompetitorProductPrice.date == today,
                CompetitorProductPrice.competitor_product_id.in_(ids),
            )
        } if ids else {}
        for row in rows.values():
            if not row.best_seller_rank:
                continue
            snap = todays.get(row.id)
            if snap is None:
                snap = CompetitorProductPrice(
                    competitor_product_id=row.id,
                    date=today,
                    price_min=row.price_min,
                    price_max=row.price_max,
                    available=row.available,
                )
                session.add(snap)
            snap.best_seller_rank = row.best_seller_rank

    detail = {
        "competitor": name,
        "ranked": ranked_rows,
        "in_list_but_not_in_catalogue": unknown,
        "top": ranked[:5],
    }
    if ranked_rows == 0 and unknown:
        # Every handle in their best-seller list is absent from our copy of
        # their catalogue, so nothing was ranked. Reporting `ok, 0 rows` here
        # would read as "nothing to do" when the truth is "the catalogue sync
        # hasn't run for this competitor yet" — a different problem with a
        # different fix. Same trap as the keyword job's "already fresh".
        return JobResult(
            skipped=True,
            skip_reason=(
                f"{name}: none of the {unknown} best-selling products are in "
                "the catalogue snapshot yet. Run the competitor catalogue "
                "sync for this competitor first."
            ),
            detail=detail,
        )

    return JobResult(rows=ranked_rows, detail=detail)


# ── Match proposals ──────────────────────────────────────────────────


def propose_competitor_matches() -> JobResult:
    """Suggest "their product is our product" pairs for the review queue.

    Only for their products with no decision yet — a confirmed or rejected
    match is final, and re-proposing a rejection is how a review queue
    becomes something nobody opens.

    Only their *priced* products, too: an unpriced competitor product can't
    contribute a price comparison, which is the entire reason for matching.
    """
    from dashboard import matching
    from dashboard.models import CompetitorMatch, MatchStatus, ShopifyProduct

    with get_session() as session:
        decided = {
            pid
            for (pid,) in session.query(CompetitorMatch.competitor_product_id)
        }
        theirs = [
            p
            for p in session.query(CompetitorProduct).filter(
                CompetitorProduct.price_min > 0
            )
            if p.id not in decided
        ]
        if not theirs:
            return JobResult(
                rows=0,
                detail={"note": "no undecided competitor products with a price"},
            )

        ours = session.query(ShopifyProduct).all()
        if not ours:
            return JobResult(
                skipped=True,
                skip_reason=(
                    "No products of our own to match against — run the "
                    "Shopify catalogue snapshot first."
                ),
            )

        proposals = matching.propose(ours, theirs)
        for their_id, our_id, value, reason in proposals:
            session.add(
                CompetitorMatch(
                    competitor_product_id=their_id,
                    shopify_product_id=our_id,
                    status=MatchStatus.proposed.value,
                    score=value,
                    reason=reason,
                )
            )
        counts = (len(theirs), len(ours), len(proposals))

    return JobResult(
        rows=counts[2],
        detail={
            "their_products_considered": counts[0],
            "our_catalogue": counts[1],
            "proposed": counts[2],
            "min_score": matching.MIN_SCORE,
        },
    )


register(
    JobSpec(
        name="competitor_catalog",
        title="Competitor catalogue",
        description=(
            "Reads one competitor's full product list from their public "
            "Shopify feed — titles, brands and prices — and records today's "
            "price for each. One competitor per run, stalest first."
        ),
        fn=sync_competitor_catalog,
        enabled_key="jobs.competitor_catalog.enabled",
        hour_key="jobs.competitor_catalog.hour",
    )
)

register(
    JobSpec(
        name="competitor_posts",
        title="Competitor blog watch",
        description=(
            "Reads one competitor's blog feed. What they publish is which "
            "searches they intend to own, and how often says how hard "
            "they're trying."
        ),
        fn=sync_competitor_posts,
        enabled_key="jobs.competitor_posts.enabled",
        hour_key="jobs.competitor_posts.hour",
    )
)

register(
    JobSpec(
        name="competitor_bestsellers",
        title="Competitor best sellers",
        description=(
            "Their own best-selling order, the closest public signal to what "
            "actually sells. Ranks products already in the catalogue snapshot."
        ),
        fn=sync_competitor_bestsellers,
        enabled_key="jobs.competitor_bestsellers.enabled",
        hour_key="jobs.competitor_bestsellers.hour",
    )
)

register(
    JobSpec(
        name="competitor_matches",
        title="Competitor match proposals",
        description=(
            "Pairs their priced products with ours by brand, series words, "
            "thickness and wear layer. Proposals only — every match is "
            "confirmed or rejected by hand on the Competitors page."
        ),
        fn=propose_competitor_matches,
        enabled_key="jobs.competitor_matches.enabled",
        hour_key="jobs.competitor_matches.hour",
    )
)
