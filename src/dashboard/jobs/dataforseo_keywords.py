"""DataForSEO → `keyword_metric`. Market size for terms the site already ranks for.

Search Console knows how the site performs on a term and can never know how
big the term is or what a click on it is worth. DataForSEO knows the second
and nothing about the first. Joined, they rank opportunity: real volume, real
commercial intent, and already sitting at position 8-30 where one better page
moves it onto page one.

**This job spends money**, which shapes every decision in it:

  * DataForSEO bills **per request, not per keyword**, and accepts up to 1000
    terms in one call. So batching is the entire cost strategy — 700 keywords
    for $0.09 rather than 700 × $0.09.
  * A **hard budget cap** is checked against this app's own ledger before any
    call. The prepaid balance was $1.00 when this was written, about eleven
    requests, and a scheduling mistake could drain it overnight.
  * Terms already fetched recently are skipped. Search volume is a 12-month
    rolling average; re-asking weekly buys nothing.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func

from blog_pipeline.tools.dataforseo import DataForSEOClient

from dashboard import store
from dashboard.db import get_session
from dashboard.jobs.gsc import settled_through
from dashboard.jobs.registry import JobResult, JobSpec, register
from dashboard.models import ApiSpend, GscQueryDaily, KeywordMetric

log = logging.getLogger(__name__)

ENDPOINT = "keywords_data/google_ads/search_volume/live"
# What DataForSEO's own price list quotes for this endpoint. The response
# carries an authoritative `cost`; this is the fallback and the pre-flight
# estimate, since the cap has to be checked before the money is spent.
COST_PER_REQUEST = 0.09

# Volume is a rolling 12-month average. Re-asking sooner spends money to watch
# a number that moves monthly at best.
STALE_AFTER_DAYS = 45

# Positions 8-30: being shown but not clicked. Google already considers the
# site relevant, which is a far shorter path to page one than a term with no
# history at all.
STRIKING_MIN, STRIKING_MAX = 5.0, 40.0

# Google Ads rejects a keyword containing most punctuation (confirmed live:
# "what are eco-friendly flooring options?" fails with 40501, and it fails
# the whole batched task, not just itself — envelope status stays 20000 while
# every candidate in the request comes back empty with no obvious cause).
# GSC queries are real user search text and routinely contain "?", so this is
# not a hypothetical; it happened on the very first real run. Filtered here
# rather than caught after the fact, because one bad string must not be able
# to cost 700 good ones an entire request.
_VALID_KEYWORD = re.compile(r"^[a-z0-9 '&\-]+$")
_MAX_KEYWORD_CHARS = 80
_MAX_KEYWORD_WORDS = 10


def is_valid_keyword(text: str) -> bool:
    text = text.strip().lower()
    if not text or len(text) > _MAX_KEYWORD_CHARS:
        return False
    if len(text.split()) > _MAX_KEYWORD_WORDS:
        return False
    return bool(_VALID_KEYWORD.match(text))


def spend_to_date() -> float:
    with get_session() as session:
        return float(
            session.query(func.coalesce(func.sum(ApiSpend.cost_usd), 0.0))
            .filter(ApiSpend.provider == "dataforseo")
            .scalar() or 0.0
        )


def budget_remaining() -> float:
    return max(0.0, float(store.get(store.DFS_BUDGET_USD)) - spend_to_date())


def _candidate_keywords(limit: int, today: date | None = None) -> list[str]:
    """Terms worth paying to learn about, best first.

    Ordered by impressions because that is the site's own evidence of
    relevance. A term with 4,000 impressions and no clicks is a specific,
    fixable problem; a term with three impressions is noise whichever way its
    volume comes back.
    """
    today = today or date.today()
    end = settled_through(today)
    start = end - timedelta(days=27)
    fresh_cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_AFTER_DAYS)
    location = store.get(store.DFS_LOCATION_CODE)

    with get_session() as session:
        known = {
            k for (k,) in session.query(KeywordMetric.keyword).filter(
                KeywordMetric.location_code == location,
                KeywordMetric.fetched_at >= fresh_cutoff,
            ).all()
        }
        rows = (
            session.query(
                GscQueryDaily.query,
                func.sum(GscQueryDaily.impressions).label("impressions"),
                func.sum(GscQueryDaily.clicks),
                func.sum(GscQueryDaily.position * GscQueryDaily.impressions),
            )
            .filter(GscQueryDaily.date >= start, GscQueryDaily.date <= end)
            .group_by(GscQueryDaily.query)
            .order_by(func.sum(GscQueryDaily.impressions).desc())
            .all()
        )

    out: list[str] = []
    for query, impressions, _clicks, weight in rows:
        if not query or query in known:
            continue
        impressions = int(impressions or 0)
        if impressions < 10:
            continue
        position = float(weight or 0.0) / impressions if impressions else 0.0
        if not (STRIKING_MIN <= position <= STRIKING_MAX):
            continue
        # Real search text — "how to fix a squeaky floor?" — routinely fails
        # Google Ads' character rules. Skipped rather than sent: DataForSEO
        # fails the *entire* batched task on one bad keyword, so letting this
        # through would cost the other candidates their whole request.
        if not is_valid_keyword(query):
            continue
        out.append(query)
        if len(out) >= limit:
            break
    return out


def sync_dataforseo_keywords(
    client: DataForSEOClient | None = None, today: date | None = None
) -> JobResult:
    client = client or DataForSEOClient()
    if not client.enabled:
        return JobResult(
            skipped=True,
            skip_reason=(
                "DataForSEO isn't configured — set DATAFORSEO_LOGIN and "
                "DATAFORSEO_PASSWORD in .env."
            ),
        )

    location = store.get(store.DFS_LOCATION_CODE)
    remaining = budget_remaining()
    detail: dict = {
        "location_code": location,
        "budget_usd": store.get(store.DFS_BUDGET_USD),
        "spent_usd": round(spend_to_date(), 4),
        "remaining_usd": round(remaining, 4),
    }

    if remaining < COST_PER_REQUEST:
        return JobResult(
            skipped=True,
            skip_reason=(
                f"Spend cap reached: ${spend_to_date():.2f} of "
                f"${store.get(store.DFS_BUDGET_USD):.2f} used, and one request "
                f"costs ${COST_PER_REQUEST:.2f}. Raise the cap in Settings if "
                "that's intended."
            ),
            detail=detail,
        )

    keywords = _candidate_keywords(store.get(store.DFS_MAX_KEYWORDS), today)
    detail["candidates"] = len(keywords)
    if not keywords:
        return JobResult(
            rows=0,
            detail={**detail, "note": "every striking-distance term already has "
                                      "fresh volume data"},
        )

    rows = client.keyword_data(keywords, location_code=location)
    detail["returned"] = len(rows)
    if not rows:
        # Nothing is billed to the ledger here: a rejected call costs nothing,
        # and inventing a charge would be worse than under-counting. The
        # client's `last_error` carries DataForSEO's own status message, which
        # distinguishes "no volume for these terms" from "your account is
        # unverified" — the second is an owner action, not a retry.
        reason = getattr(client, "last_error", None)
        return JobResult(
            skipped=bool(reason),
            skip_reason=(
                f"DataForSEO rejected the request — {reason}"
                if reason else None
            ),
            rows=0,
            detail={**detail, "api_error": reason or "no rows returned"},
        )

    now = datetime.now(timezone.utc)
    written = 0
    with get_session() as session:
        existing = {
            k.keyword: k for k in session.query(KeywordMetric).filter(
                KeywordMetric.location_code == location,
                KeywordMetric.keyword.in_([r.get("keyword") for r in rows]),
            ).all()
        }
        for row in rows:
            keyword = (row.get("keyword") or "").strip()
            if not keyword:
                continue
            fields = {
                "search_volume": row.get("search_volume"),
                "competition": row.get("competition"),
                "cpc": row.get("cpc"),
                "fetched_at": now,
            }
            current = existing.get(keyword)
            if current is None:
                session.add(KeywordMetric(
                    keyword=keyword, location_code=location, **fields
                ))
            else:
                for key, value in fields.items():
                    setattr(current, key, value)
            written += 1

        session.add(ApiSpend(
            provider="dataforseo", endpoint=ENDPOINT, requests=1,
            cost_usd=COST_PER_REQUEST, items=written, created_at=now,
        ))

    detail["spent_usd"] = round(spend_to_date(), 4)
    detail["remaining_usd"] = round(budget_remaining(), 4)
    detail["cost_this_run_usd"] = COST_PER_REQUEST
    detail["with_volume"] = sum(1 for r in rows if r.get("search_volume"))
    return JobResult(rows=written, detail=detail)


register(
    JobSpec(
        name="dataforseo_keywords",
        title="Keyword market data",
        description=(
            "Batches striking-distance search terms into one DataForSEO "
            "request for volume, CPC and competition. Bills per request, so "
            "up to 700 keywords cost the same $0.09 as one. Refuses to call "
            "once the spend cap is reached."
        ),
        fn=sync_dataforseo_keywords,
        enabled_key="jobs.dataforseo_keywords.enabled",
        hour_key="jobs.dataforseo_keywords.hour",
        # A paid call must never be retried automatically: the client returns
        # [] on failure rather than raising, so a retry here would be a second
        # charge for the same unanswered question.
        max_attempts=1,
    )
)
