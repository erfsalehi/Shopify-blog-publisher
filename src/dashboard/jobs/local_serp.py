"""City-level rank tracking → `local_serp_rank`.

Search Console reports one national average position per query. For a
business with a single showroom that number is close to a lie by omission:
measured live, `flooring langley` sits at **position 16 nationally and
position 2 inside Langley**. Acting on the 16 would mean rewriting a page
that is already nearly first where it matters.

The same run also records the **local pack**, which is a separate
competition from the blue links and usually the one that produces the phone
call. On the first live run the site was 2nd organically and *absent from the
pack entirely* — two facts that point at completely different work, and
neither is visible in Search Console.

Billed per SERP, so it is budget-gated through the same `api_spend` ledger as
the keyword job, and the cost is read back from DataForSEO's own response
rather than assumed.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone

from blog_pipeline.tools.dataforseo import DataForSEOClient

from dashboard import store
from dashboard.db import get_session
from dashboard.jobs.dataforseo_keywords import is_valid_keyword
from dashboard.jobs.registry import JobResult, JobSpec, register
from dashboard.models import ApiSpend, GscQueryDaily, LocalSerpRank

_ENDPOINT = "serp/google/organic/live/advanced"


def local_spend_to_date() -> float:
    """Only what local rank tracking has cost.

    Separate from the keyword job's total on purpose: the two have their own
    caps, and a shared number would let a keyword refresh silently consume
    the rank-tracking budget for the month.
    """
    from sqlalchemy import func

    with get_session() as session:
        return float(
            session.query(func.coalesce(func.sum(ApiSpend.cost_usd), 0.0))
            .filter(ApiSpend.endpoint == _ENDPOINT)
            .scalar() or 0.0
        )

log = logging.getLogger(__name__)

#: Our own domain, as DataForSEO reports it in `domain`.
OUR_DOMAIN = "drflooring.ca"

#: What a SERP costs. Read from the response when present; this is only the
#: pre-flight estimate used to decide whether to call at all.
ESTIMATED_COST = 0.002


def _cities() -> list[tuple[str, int]]:
    """(name, DataForSEO location_code) pairs to search from.

    Codes confirmed against DataForSEO's own CA location list rather than
    guessed — a wrong code silently returns a SERP for somewhere else, which
    is the kind of error that looks like data.
    """
    raw = store.get(store.LOCAL_CITIES) or []
    out: list[tuple[str, int]] = []
    for item in raw:
        name, _, code = str(item).partition(":")
        code = code.strip()
        if name.strip() and code.isdigit():
            out.append((name.strip(), int(code)))
    return out


def _keywords(limit: int) -> list[str]:
    """Local-intent terms worth tracking, best first.

    Sourced from Search Console rather than invented: a term the site already
    gets impressions for is one Google associates with us, and tracking a
    keyword nobody searches burns budget to learn nothing. Filtered to terms
    that name a place, because a national term measured from Langley is just
    the national term.
    """
    seeds = [k.strip().lower() for k in (store.get(store.LOCAL_SEEDS) or []) if k.strip()]
    places = tuple(
        name.lower() for name, _ in _cities()
    ) + ("langley", "surrey", "abbotsford", "cloverdale", "fraser valley", "bc",
         "near me")

    with get_session() as session:
        rows = (
            session.query(GscQueryDaily.query)
            .distinct()
            .all()
        )
    found: list[str] = []
    for (query,) in rows:
        text = (query or "").strip().lower()
        if not text or text in found or not is_valid_keyword(text):
            continue
        if any(place in text for place in places):
            found.append(text)

    # Seeds first: they're the terms the owner decided matter, and they must
    # not be crowded out by whatever Search Console happens to rank today.
    ordered = [s for s in seeds if is_valid_keyword(s)]
    ordered += [k for k in found if k not in ordered]
    return ordered[:limit]


def sync_local_serp(
    client: DataForSEOClient | None = None, today: date | None = None
) -> JobResult:
    client = client or DataForSEOClient()
    today = today or date.today()

    if not client.enabled:
        return JobResult(
            skipped=True,
            skip_reason=(
                "DataForSEO isn't configured — set DATAFORSEO_LOGIN and "
                "DATAFORSEO_PASSWORD in .env."
            ),
        )

    cities = _cities()
    if not cities:
        return JobResult(
            skipped=True,
            skip_reason=(
                "No cities configured for local rank tracking. Set them on "
                "the Settings page as Name:location_code pairs."
            ),
        )

    budget = float(store.get(store.LOCAL_BUDGET_USD) or 0.0)
    spent = local_spend_to_date()
    remaining = budget - spent
    per_run = int(store.get(store.LOCAL_MAX_KEYWORDS) or 10)

    detail: dict = {
        "cities": [name for name, _ in cities],
        "budget_usd": round(budget, 4),
        "spent_usd": round(spent, 4),
        "remaining_usd": round(remaining, 4),
    }

    if remaining < ESTIMATED_COST:
        return JobResult(
            skipped=True,
            skip_reason=(
                f"Local rank budget reached: ${spent:.2f} of ${budget:.2f} "
                "used. Raise the cap in Settings if that's intended."
            ),
            detail=detail,
        )

    keywords = _keywords(per_run)
    detail["keywords"] = len(keywords)
    if not keywords:
        return JobResult(
            skipped=True,
            skip_reason=(
                "No local-intent search terms found. Run the Search Console "
                "sync first, or add seed keywords in Settings."
            ),
            detail=detail,
        )

    # One SERP per keyword per city, cheapest-first ordering so a budget that
    # runs out mid-run has spent it on the terms the owner named.
    now = datetime.now(timezone.utc)
    calls = written = in_pack = 0
    cost_total = 0.0
    failures: list[str] = []

    for keyword in keywords:
        for city_name, code in cities:
            if (remaining - cost_total) < ESTIMATED_COST:
                break
            result = client.local_serp(keyword, location_code=code)
            calls += 1
            if result is None:
                failures.append(f"{keyword} @ {city_name}: {client.last_error}")
                continue
            cost_total += float(result.get("cost") or 0.0) or ESTIMATED_COST

            items = result.get("items") or []
            organic = [i for i in items if i.get("type") == "organic"]
            pack = [i for i in items if i.get("type") == "local_pack"]

            ours = next(
                (i for i in organic if OUR_DOMAIN in (i.get("domain") or "")), None
            )
            ours_pack = next(
                (i for i in pack if OUR_DOMAIN in json.dumps(i).lower()), None
            )
            if ours_pack:
                in_pack += 1

            with get_session() as session:
                session.query(LocalSerpRank).filter(
                    LocalSerpRank.date == today,
                    LocalSerpRank.keyword == keyword,
                    LocalSerpRank.city == city_name,
                ).delete(synchronize_session=False)
                session.add(LocalSerpRank(
                    date=today,
                    keyword=keyword,
                    city=city_name,
                    # None, not a big number: "not in the results" is a
                    # different fact from "ranked 100th", and averaging a
                    # placeholder would invent a position we never held.
                    position=ours.get("rank_group") if ours else None,
                    pack_position=(
                        ours_pack.get("rank_group") if ours_pack else None
                    ),
                    top_domains_json=json.dumps(
                        [i.get("domain") for i in organic[:5] if i.get("domain")]
                    ),
                    pack_names_json=json.dumps(
                        [str(i.get("title") or "")[:120] for i in pack[:3]]
                    ),
                    results_seen=len(organic),
                    fetched_at=now,
                ))
                written += 1

    if cost_total > 0:
        with get_session() as session:
            session.add(ApiSpend(
                provider="dataforseo",
                endpoint=_ENDPOINT,
                requests=calls,
                cost_usd=round(cost_total, 6),
                items=written,
            ))

    detail.update({
        "serp_calls": calls,
        "rows": written,
        "cost_usd": round(cost_total, 4),
        "in_local_pack": in_pack,
    })
    if failures:
        detail["failures"] = failures[:5]
    return JobResult(rows=written, detail=detail)


register(
    JobSpec(
        name="local_serp",
        title="Local rank tracking",
        description=(
            "Where the site ranks for local terms searched from inside "
            "Langley and the neighbouring cities, organic and local pack. "
            "Search Console only reports a national average, which for one "
            "showroom hides the answer completely."
        ),
        fn=sync_local_serp,
        enabled_key="jobs.local_serp.enabled",
        hour_key="jobs.local_serp.hour",
    )
)
