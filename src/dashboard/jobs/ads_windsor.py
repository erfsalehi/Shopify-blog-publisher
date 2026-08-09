"""Google Ads → `ads_campaign_daily`, via Windsor.ai's REST API.

PLAN.md Phase 4, Route A. Windsor already has the D&R Flooring account
connected, which makes paid performance available today rather than after the
developer-token review.

**This is not the Windsor MCP connector.** That one is a tool inside Claude's
session; a job running at 07:00 on this machine cannot reach it. Same Windsor
account, different door: this uses the documented REST endpoint at
connectors.windsor.ai with an API key from
https://onboard.windsor.ai/app/data-preview.

Route B (the direct Google Ads API) will land alongside this rather than
replace it, which is why every row records its `source`. During the changeover
both pipes run, and a spend figure whose origin is ambiguous is a spend figure
nobody trusts.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import httpx

from dashboard import store
from dashboard.config import get_settings
from dashboard.db import get_session
from dashboard.jobs.registry import JobResult, JobSpec, register
from dashboard.jobs.windows import plan_window
from dashboard.models import AdsCampaignDaily, GscFetchDay

log = logging.getLogger(__name__)

API = "https://connectors.windsor.ai/google_ads"
SOURCE = "windsor"
_KIND = "ads_windsor"

# Google keeps revising conversion counts as attribution windows close, and
# Windsor mirrors whatever Google currently reports. Spend settles faster than
# conversions do; this covers the slower of the two.
SETTLING_DAYS = 3

_FIELDS = (
    "date", "campaign", "campaign_type", "campaign_status",
    "spend", "clicks", "impressions", "conversions",
)


def settled_through(today: date | None = None) -> date:
    return (today or date.today()) - timedelta(days=SETTLING_DAYS)


class WindsorError(RuntimeError):
    pass


class WindsorClient:
    """Minimal REST client. Only the one endpoint this job needs."""

    def __init__(self, api_key: str | None = None):
        self.api_key = (
            api_key if api_key is not None else get_settings().windsor_api_key
        ).strip()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def fetch(self, start: date, end: date) -> list[dict]:
        resp = httpx.get(
            API,
            params={
                "api_key": self.api_key,
                "fields": ",".join(_FIELDS),
                "date_from": start.isoformat(),
                "date_to": end.isoformat(),
            },
            timeout=120.0,
        )
        if resp.status_code in (401, 403):
            raise WindsorError(
                "Windsor rejected the API key (HTTP "
                f"{resp.status_code}). Check WINDSOR_API_KEY against "
                "https://onboard.windsor.ai/app/data-preview — this is an "
                "auth failure, not a transient one, so it is not retried."
            )
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get("data", payload.get("result", payload))
        if not isinstance(rows, list):
            raise WindsorError(
                f"Unexpected Windsor response shape: {type(rows).__name__}. "
                f"Keys were {sorted(payload)[:8] if isinstance(payload, dict) else '?'}"
            )
        return rows


def _parse_day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _fetched_days(session) -> set[date]:
    return {
        d for (d,) in session.query(GscFetchDay.date)
        .filter(GscFetchDay.kind == _KIND).all()
    }


def _mark_fetched(session, start: date, end: date) -> None:
    session.query(GscFetchDay).filter(
        GscFetchDay.kind == _KIND,
        GscFetchDay.date >= start,
        GscFetchDay.date <= end,
    ).delete(synchronize_session=False)
    now = datetime.now(timezone.utc)
    day = start
    while day <= end:
        session.add(GscFetchDay(date=day, kind=_KIND, fetched_at=now))
        day += timedelta(days=1)


def sync_ads_windsor(
    client: WindsorClient | None = None, today: date | None = None
) -> JobResult:
    client = client or WindsorClient()
    if not client.enabled:
        return JobResult(
            skipped=True,
            skip_reason=(
                "Windsor isn't configured — add WINDSOR_API_KEY to .env. Get it "
                "from https://onboard.windsor.ai/app/data-preview. Note the "
                "Windsor MCP connector is a separate thing and can't be used by "
                "a scheduled job."
            ),
        )

    today = today or date.today()
    backfill = store.get(store.ADS_BACKFILL_DAYS)
    recent = store.get(store.ADS_RECENT_DAYS)

    with get_session() as session:
        have = _fetched_days(session)

    _, _, wanted = plan_window(
        have, backfill_days=backfill, recent_days=recent, today=today
    )
    detail: dict = {
        "source": SOURCE,
        "settled_through": settled_through(today).isoformat(),
        "api_calls": 0,
    }
    if not wanted:
        return JobResult(rows=0, detail={**detail, "note": "already up to date"})

    start, end = min(wanted), max(wanted)
    rows = client.fetch(start, end)
    detail["api_calls"] = 1
    detail["window"] = f"{start.isoformat()}..{end.isoformat()}"
    detail["rows_returned"] = len(rows)

    written = 0
    campaigns: set[str] = set()
    spend = 0.0
    now = datetime.now(timezone.utc)
    seen: set[tuple[date, str]] = set()

    with get_session() as session:
        # Scoped to this source: when the direct Google Ads API lands, its
        # rows for the same days must survive a Windsor sync.
        session.query(AdsCampaignDaily).filter(
            AdsCampaignDaily.source == SOURCE,
            AdsCampaignDaily.date >= start,
            AdsCampaignDaily.date <= end,
        ).delete(synchronize_session=False)

        for row in rows:
            day = _parse_day(row.get("date"))
            campaign = (row.get("campaign") or "").strip()
            if day is None or not campaign or not (start <= day <= end):
                continue
            if (day, campaign) in seen:
                continue
            seen.add((day, campaign))
            campaigns.add(campaign)
            spend += _num(row.get("spend"))
            session.add(AdsCampaignDaily(
                date=day,
                campaign=campaign,
                campaign_type=row.get("campaign_type") or None,
                campaign_status=row.get("campaign_status") or None,
                spend=_num(row.get("spend")),
                clicks=int(_num(row.get("clicks"))),
                impressions=int(_num(row.get("impressions"))),
                # Kept fractional: Google attributes partial conversions and
                # this account really does report 1.5 and 2.5. Rounding here
                # would change the numbers on the way in.
                conversions=_num(row.get("conversions")),
                source=SOURCE,
                fetched_at=now,
            ))
            written += 1
        _mark_fetched(session, start, end)

    detail["campaigns"] = sorted(campaigns)
    detail["spend_in_window"] = round(spend, 2)
    return JobResult(rows=written, detail=detail)


register(
    JobSpec(
        name="ads_windsor",
        title="Google Ads sync (Windsor)",
        description=(
            "Daily campaign spend, clicks, impressions and conversions via "
            "Windsor.ai's REST API. Available now, without waiting on the "
            "Google Ads developer token."
        ),
        fn=sync_ads_windsor,
        enabled_key="jobs.ads_windsor.enabled",
        hour_key="jobs.ads_windsor.hour",
    )
)
