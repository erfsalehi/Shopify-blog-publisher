"""Search Console sync, plus the two questions the data exists to answer.

sync_performance() pulls a window of page- and query-level rows and stores
them as an immutable snapshot. The two readers:

  * striking_distance_queries() — terms already earning impressions from
    positions 11-30. The site is being shown and not clicked; one better
    article can move those onto page one. This is where a 1.67% CTR at 335k
    impressions actually lives.
  * decaying_articles() — live posts whose impressions fell between two
    windows. Ranks refresh candidates by measured decay rather than by age,
    which is a proxy for it at best.
"""

from __future__ import annotations

from datetime import date, timedelta

from blog_pipeline.db import AiReferral, Article, SearchPerformance, get_session
from blog_pipeline.db.models import ArticleStatus
from blog_pipeline.tools.analytics import AnalyticsClient, is_ai_source
from blog_pipeline.tools.search_console import SearchConsoleClient, default_window


def _normalize(url: str | None) -> str:
    """Compare URLs ignoring scheme, www and trailing slash.

    Search Console reports the canonical URL, which won't necessarily match
    the string we stored character-for-character — and a join that silently
    matches nothing looks exactly like a site with no traffic.
    """
    if not url:
        return ""
    u = str(url).strip().lower()
    for prefix in ("https://", "http://"):
        if u.startswith(prefix):
            u = u[len(prefix):]
    if u.startswith("www."):
        u = u[4:]
    return u.rstrip("/")


def _build_rows(
    pages: list[dict], queries: list[dict], by_url: dict[str, int],
    start: date, end: date,
) -> tuple[list[SearchPerformance], int]:
    rows: list[SearchPerformance] = []
    matched = 0
    for row in pages:
        url = (row.get("keys") or [None])[0]
        article_id = by_url.get(_normalize(url))
        if article_id:
            matched += 1
        rows.append(
            SearchPerformance(
                article_id=article_id, page=url, query=None,
                clicks=int(row.get("clicks", 0)),
                impressions=int(row.get("impressions", 0)),
                ctr=float(row.get("ctr", 0.0)),
                position=float(row.get("position", 0.0)),
                period_start=start, period_end=end,
            )
        )
    for row in queries:
        rows.append(
            SearchPerformance(
                article_id=None, page=None,
                query=(row.get("keys") or [None])[0],
                clicks=int(row.get("clicks", 0)),
                impressions=int(row.get("impressions", 0)),
                ctr=float(row.get("ctr", 0.0)),
                position=float(row.get("position", 0.0)),
                period_start=start, period_end=end,
            )
        )
    return rows, matched


def _prune_old_windows(session, keep: int) -> int:
    """Drop all but the `keep` most recent snapshots.

    One sync of this site stores ~31k rows, and a weekly cron adds two fresh
    windows every run — roughly 240MB a year, against a 500MB free tier, and
    it never stops growing. The readers only ever look at the two most recent
    windows, so older snapshots cost storage and buy nothing. Keeping four
    leaves a spare pair in case a sync fails.
    """
    ends = [
        e[0]
        for e in session.query(SearchPerformance.period_end)
        .distinct()
        .order_by(SearchPerformance.period_end.desc())
        .all()
    ]
    stale = ends[keep:]
    if not stale:
        return 0
    return (
        session.query(SearchPerformance)
        .filter(SearchPerformance.period_end.in_(stale))
        .delete(synchronize_session=False)
    )


def sync_performance(
    *,
    days: int = 90,
    compare: bool = True,
    retain_windows: int = 4,
    dry_run: bool = False,
) -> dict:
    """Pull the current window, and by default the preceding one too.

    Fetching both in one run is what makes decay measurable immediately.
    Syncing a single window weekly would instead leave two 90-day windows
    overlapping by ~92%, whose difference is noise — you'd wait months for a
    usable trend. Two adjacent, non-overlapping windows give it on the first
    run.
    """
    client = SearchConsoleClient()
    if not client.enabled:
        return {"enabled": False, "pages": 0, "queries": 0, "matched": 0}

    start, end = default_window(days)
    windows = [(start, end)]
    if compare:
        windows.append((start - timedelta(days=days), start))

    total_pages = total_queries = matched = 0
    with get_session() as session:
        by_url = {
            _normalize(a.shopify_url): a.id
            for a in session.query(Article)
            .filter(Article.shopify_url.isnot(None))
            .all()
        }
        for i, (w_start, w_end) in enumerate(windows):
            pages = client.query(dimensions=["page"], start_date=w_start, end_date=w_end)
            queries = client.query(
                dimensions=["query"], start_date=w_start, end_date=w_end
            )
            rows, hit = _build_rows(pages, queries, by_url, w_start, w_end)
            total_pages += len(pages)
            total_queries += len(queries)
            if i == 0:  # only the current window's match rate is interesting
                matched = hit
            if not dry_run:
                # Replace any existing snapshot for this exact window, so
                # re-running a sync doesn't double-count it.
                session.query(SearchPerformance).filter(
                    SearchPerformance.period_start == w_start,
                    SearchPerformance.period_end == w_end,
                ).delete(synchronize_session=False)
                session.add_all(rows)

        pruned = 0
        if not dry_run and retain_windows > 0:
            session.flush()  # so this run's windows count as recent
            pruned = _prune_old_windows(session, retain_windows)

    return {
        "enabled": True,
        "window": f"{start.isoformat()}..{end.isoformat()}",
        "compared_to": (
            f"{windows[1][0].isoformat()}..{windows[1][1].isoformat()}"
            if compare else None
        ),
        "pages": total_pages,
        "queries": total_queries,
        "matched": matched,
        "pruned_rows": pruned,
        "dry_run": dry_run,
    }


def sync_ai_referrals(*, days: int = 90, dry_run: bool = False) -> dict:
    """Store sessions that arrived from an AI assistant.

    GA4 reports the landing page as a path ("/blogs/news/x"), while articles
    carry an absolute URL — _normalize strips the scheme and host from both, so
    the path is compared against the article URL's tail rather than failing to
    match every row.
    """
    client = AnalyticsClient()
    if not client.enabled:
        return {"enabled": False, "ai_sessions": 0, "sources": 0}

    start, end = default_window(days)
    rows = client.run_report(
        dimensions=["sessionSource", "landingPagePlusQueryString"],
        metrics=["sessions", "activeUsers"],
        start_date=start,
        end_date=end,
    )

    ai_rows = [r for r in rows if is_ai_source((r["dimensions"] or [None])[0])]
    total_sessions = 0
    with get_session() as session:
        by_path = {
            _path_of(a.shopify_url): a.id
            for a in session.query(Article)
            .filter(Article.shopify_url.isnot(None))
            .all()
        }
        records = []
        for row in ai_rows:
            source = row["dimensions"][0]
            landing = row["dimensions"][1] if len(row["dimensions"]) > 1 else None
            sessions = int(row["metrics"][0] or 0)
            users = int(row["metrics"][1] or 0) if len(row["metrics"]) > 1 else 0
            total_sessions += sessions
            records.append(
                AiReferral(
                    article_id=by_path.get(_path_of(landing)),
                    source=source,
                    landing_page=landing,
                    sessions=sessions,
                    users=users,
                    period_start=start,
                    period_end=end,
                )
            )
        if not dry_run:
            session.query(AiReferral).filter(
                AiReferral.period_start == start, AiReferral.period_end == end
            ).delete(synchronize_session=False)
            session.add_all(records)

    return {
        "enabled": True,
        "window": f"{start.isoformat()}..{end.isoformat()}",
        "rows_scanned": len(rows),
        "ai_rows": len(ai_rows),
        "ai_sessions": total_sessions,
        "sources": len({r["dimensions"][0] for r in ai_rows}),
        "dry_run": dry_run,
    }


def ai_referral_summary(*, limit: int = 10) -> dict:
    """AI sessions in the latest window, by source and by landing page."""
    with get_session() as session:
        latest = (
            session.query(AiReferral.period_end)
            .order_by(AiReferral.period_end.desc())
            .first()
        )
        if not latest:
            return {"available": False}
        rows = (
            session.query(AiReferral)
            .filter(AiReferral.period_end == latest[0])
            .all()
        )
        by_source: dict[str, int] = {}
        by_page: dict[str, int] = {}
        for r in rows:
            by_source[r.source] = by_source.get(r.source, 0) + r.sessions
            if r.landing_page:
                by_page[r.landing_page] = by_page.get(r.landing_page, 0) + r.sessions
        return {
            "available": True,
            "total_sessions": sum(by_source.values()),
            "sources": sorted(by_source.items(), key=lambda kv: -kv[1])[:limit],
            "pages": sorted(by_page.items(), key=lambda kv: -kv[1])[:limit],
        }


def _path_of(url: str | None) -> str:
    """The path part of a URL, normalized. GA4 reports landing pages as paths
    while articles store absolute URLs; this makes the two comparable."""
    normalized = _normalize(url)
    if not normalized:
        return ""
    slash = normalized.find("/")
    return normalized[slash:] if slash != -1 else "/"


def _two_windows(session, dimension_col) -> tuple[date | None, date | None]:
    """The most recent snapshot, and the most recent one that does NOT overlap
    it — i.e. the newest window ending on or before the current one starts.

    Taking simply "the two most recent" is wrong the moment sync-performance
    runs on two different days: each run stores a 90-day window ending 3 days
    back, so a Monday and a Thursday run leave windows 3 days apart that share
    87 days of data. Their difference is a few days of noise, but it reads as
    decay and ranks like it — that is how a refresh run came to pick a random
    article over one that had lost 12,775 impressions.

    Returns (current_end, previous_end); previous is None when no
    non-overlapping window exists, which callers treat as "no trend yet".
    """
    windows = [
        (w[0], w[1])
        for w in session.query(
            SearchPerformance.period_start, SearchPerformance.period_end
        )
        .filter(dimension_col.isnot(None))
        .distinct()
        .order_by(SearchPerformance.period_end.desc())
        .all()
    ]
    if not windows:
        return (None, None)
    current_start, current_end = windows[0]
    for start, end in windows[1:]:
        if end <= current_start:
            return (current_end, end)
    return (current_end, None)


def site_summary() -> dict:
    """Site-wide totals for the current window, and the change from the one
    before it.

    Clicks and impressions are summed from page rows; averaging a CTR column
    across pages would weight a 3-impression page the same as a 3,000-one, so
    it's recomputed from the totals instead.
    """
    with get_session() as session:
        current, previous = _two_windows(session, SearchPerformance.page)
        if current is None:
            return {"available": False}

        def _totals(period):
            rows = (
                session.query(SearchPerformance)
                .filter(
                    SearchPerformance.period_end == period,
                    SearchPerformance.page.isnot(None),
                )
                .all()
            )
            clicks = sum(r.clicks for r in rows)
            impressions = sum(r.impressions for r in rows)
            # Position weighted by impressions: an unweighted mean is dominated
            # by the long tail of pages nobody sees.
            weighted = sum(r.position * r.impressions for r in rows)
            return {
                "clicks": clicks,
                "impressions": impressions,
                "ctr": (clicks / impressions) if impressions else 0.0,
                "position": (weighted / impressions) if impressions else 0.0,
                "pages": len(rows),
            }

        now = _totals(current)
        out = {"available": True, "window_end": current, **now}
        if previous is not None:
            before = _totals(previous)
            out["previous"] = before
            for key in ("clicks", "impressions"):
                prior = before[key]
                out[f"{key}_change_pct"] = (
                    round(100 * (now[key] - prior) / prior, 1) if prior else None
                )
        return out


def top_pages(*, limit: int = 10) -> list[dict]:
    """Highest-impression pages in the latest window, article or not."""
    with get_session() as session:
        current, _ = _two_windows(session, SearchPerformance.page)
        if current is None:
            return []
        rows = (
            session.query(SearchPerformance)
            .filter(
                SearchPerformance.period_end == current,
                SearchPerformance.page.isnot(None),
            )
            .order_by(SearchPerformance.impressions.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "page": r.page,
                "impressions": r.impressions,
                "clicks": r.clicks,
                "ctr": round(r.ctr, 4),
                "position": round(r.position, 1),
                "is_article": r.article_id is not None,
            }
            for r in rows
        ]


def striking_distance_queries(
    *,
    limit: int = 25,
    min_impressions: int = 50,
    position_band: tuple[float, float] = (8.0, 30.0),
) -> list[dict]:
    """Queries with real demand that the site nearly ranks for.

    Position 8-30 is the band worth writing for: above ~8 you already win the
    click, past ~30 a single article won't close the gap. Ordered by
    impressions, because that's the traffic actually on the table.
    """
    low, high = position_band
    with get_session() as session:
        latest = (
            session.query(SearchPerformance.period_end)
            .filter(SearchPerformance.query.isnot(None))
            .order_by(SearchPerformance.period_end.desc())
            .first()
        )
        if not latest:
            return []
        rows = (
            session.query(SearchPerformance)
            .filter(
                SearchPerformance.period_end == latest[0],
                SearchPerformance.query.isnot(None),
                SearchPerformance.impressions >= min_impressions,
                SearchPerformance.position >= low,
                SearchPerformance.position <= high,
            )
            .order_by(SearchPerformance.impressions.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "query": r.query,
                "impressions": r.impressions,
                "clicks": r.clicks,
                "position": round(r.position, 1),
                "ctr": round(r.ctr, 4),
            }
            for r in rows
        ]


def decaying_articles(*, limit: int = 10, min_impressions: int = 20) -> list[dict]:
    """Live articles whose impressions dropped between the current window and
    the last non-overlapping one, ranked by how much traffic was actually lost.

    Ordered by absolute impressions lost, NOT by percentage. Percentage
    flatters trivia: an article falling 25 -> 1 is a 96% collapse worth 24
    impressions, while one falling 18,272 -> 5,497 is "only" -70% and worth
    12,775. Ranking by percent put the former first and dropped the latter off
    a top-3 entirely. Refresh spends an LLM call and edits a live page per
    candidate, so it should spend them where the traffic is.

    Needs two syncs to say anything — with one window there's no trend, and
    the honest answer is an empty list rather than a guess.
    """
    with get_session() as session:
        # Via _two_windows so the comparison can't be against an overlapping
        # snapshot — see its docstring. This used to pick "the two most recent"
        # itself, which silently compared windows a day apart once a second
        # sync ran, and ranked a few days of noise as decay.
        current, previous = _two_windows(session, SearchPerformance.page)
        if current is None or previous is None:
            return []

        def _by_article(period):
            return {
                r.article_id: r
                for r in session.query(SearchPerformance)
                .filter(
                    SearchPerformance.period_end == period,
                    SearchPerformance.article_id.isnot(None),
                )
                .all()
            }

        now, before = _by_article(current), _by_article(previous)
        out = []
        for article_id, row in now.items():
            prior = before.get(article_id)
            if prior is None or prior.impressions < min_impressions:
                continue
            delta = row.impressions - prior.impressions
            if delta >= 0:
                continue
            article = session.get(Article, article_id)
            if article is None or article.status is not ArticleStatus.published:
                continue
            out.append(
                {
                    "article_id": article_id,
                    "title": article.title,
                    "impressions_now": row.impressions,
                    "impressions_before": prior.impressions,
                    "impressions_lost": -delta,
                    "change_pct": round(100 * delta / prior.impressions, 1),
                    "position": round(row.position, 1),
                }
            )
        out.sort(key=lambda r: r["impressions_lost"], reverse=True)
        return out[:limit]
