"""Google Analytics 4 client — did an AI answer actually send anyone here?

Search Console covers Google Search and nothing else: it has no idea ChatGPT
exists, and it folds AI Overviews into ordinary Search rows with no way to
isolate them. GA4 referrals are the one place a click from an AI assistant is
directly observable — ChatGPT tags its outbound links `utm_source=chatgpt.com`,
and Perplexity/Claude/Copilot arrive as plain referrers.

This is ground truth, with one honest limit: it only sees citations that
produced a click. Being quoted to someone who never clicks is real value and
completely invisible here.

Auth mirrors search_console.py — a service account, with its email added as a
Viewer under GA4 Admin → Property access management. Being an owner of the
property in your own browser grants the robot nothing.
"""

from __future__ import annotations

from datetime import date

import httpx

from blog_pipeline.config import get_settings

DATA_API = "https://analyticsdata.googleapis.com/v1beta"
ADMIN_API = "https://analyticsadmin.googleapis.com/v1beta"
SCOPE = "https://www.googleapis.com/auth/analytics.readonly"

# Hosts that mean "an AI assistant sent this person". Deliberately explicit
# rather than pattern-matched: `bing.com` and `google.com` carry both ordinary
# search and AI answers with no way to tell them apart from the referrer, so
# counting them would quietly inflate every number here. Better to under-report
# than to claim credit for organic search.
AI_SOURCES = frozenset(
    {
        "chatgpt.com",
        "chat.openai.com",
        "openai.com",
        "perplexity.ai",
        "www.perplexity.ai",
        "claude.ai",
        "gemini.google.com",
        "bard.google.com",
        "copilot.microsoft.com",
        "you.com",
        "poe.com",
        "phind.com",
        "grok.com",
        "meta.ai",
        "duckduckgo.com/aichat",
    }
)


class AnalyticsError(RuntimeError):
    pass


def is_ai_source(source: str | None) -> bool:
    """True when a GA4 sessionSource is an AI assistant.

    Matches the bare host and any `www.` form, but never a substring — a
    referrer of "notchatgpt.com.example" is not ChatGPT.
    """
    if not source:
        return False
    s = source.strip().lower().removeprefix("www.")
    return s in AI_SOURCES or f"www.{s}" in AI_SOURCES


class AnalyticsClient:
    def __init__(self, credentials_json: str | None = None, property_id: str | None = None):
        s = get_settings()
        self._credentials_json = (
            credentials_json if credentials_json is not None else s.ga4_credentials
        )
        self.property_id = str(property_id or s.ga4_property_id).strip()
        self._cached_token: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._credentials_json and self.property_id)

    def _token(self) -> str:
        if self._cached_token:
            return self._cached_token
        try:
            import json

            from google.auth.transport.requests import Request
            from google.oauth2 import service_account
        except ImportError as e:  # pragma: no cover - depends on the extra
            raise AnalyticsError(
                "GA4 needs the [gsc] extra: pip install -e '.[gsc]'"
            ) from e
        try:
            info = json.loads(self._credentials_json)
        except ValueError as e:
            raise AnalyticsError(
                "The GA4 credentials aren't valid JSON — paste the service "
                "account key whole, including the outer braces."
            ) from e
        try:
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=[SCOPE]
            )
            creds.refresh(Request())
        except Exception as e:
            raise AnalyticsError(f"Could not authenticate to Google: {e}") from e
        self._cached_token = creds.token
        return self._cached_token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}"}

    def list_properties(self) -> list[dict]:
        """Properties this service account can read, as
        {property_id, display_name, account}.

        The setup diagnostic, and the only easy way to find the numeric id:
        GA4's UI shows it in Admin → Property Settings, but people reach for
        the G-XXXXXXX measurement id instead, which the Data API rejects.
        """
        if not self._credentials_json:
            return []
        resp = httpx.get(
            f"{ADMIN_API}/accountSummaries", headers=self._headers(), timeout=30.0
        )
        if resp.status_code == 403:
            raise AnalyticsError(
                "403 listing properties. Enable the Google Analytics Admin API "
                "in the Cloud project, and add the service account under GA4 "
                "Admin → Property access management."
            )
        resp.raise_for_status()
        out = []
        for account in resp.json().get("accountSummaries", []):
            for prop in account.get("propertySummaries", []):
                # "properties/493820114" -> "493820114"
                out.append(
                    {
                        "property_id": prop.get("property", "").split("/")[-1],
                        "display_name": prop.get("displayName"),
                        "account": account.get("displayName"),
                    }
                )
        return out

    def run_report(
        self,
        *,
        dimensions: list[str],
        metrics: list[str],
        start_date: date,
        end_date: date,
        limit: int = 50000,
    ) -> list[dict]:
        """runReport rows as {dimensions: [...], metrics: [...]}."""
        if not self.enabled:
            return []
        resp = httpx.post(
            f"{DATA_API}/properties/{self.property_id}:runReport",
            headers=self._headers(),
            json={
                "dimensions": [{"name": d} for d in dimensions],
                "metrics": [{"name": m} for m in metrics],
                "dateRanges": [
                    {
                        "startDate": start_date.isoformat(),
                        "endDate": end_date.isoformat(),
                    }
                ],
                "limit": limit,
            },
            timeout=60.0,
        )
        if resp.status_code == 403:
            raise AnalyticsError(
                f"403 for property {self.property_id}. Add the service account's "
                "client_email as a Viewer under GA4 Admin → Property access "
                "management — owning the property yourself grants it nothing. "
                "`sync-analytics --list-properties` shows what it can see."
            )
        if resp.status_code in (400, 404):
            raise AnalyticsError(
                f"{resp.status_code} for property {self.property_id!r}. This must "
                "be the NUMERIC property id from Admin → Property Settings "
                "(e.g. 493820114), not the G-XXXXXXX measurement id from the "
                "tracking snippet. Try --list-properties."
            )
        resp.raise_for_status()
        data = resp.json()
        return [
            {
                "dimensions": [d.get("value") for d in row.get("dimensionValues", [])],
                "metrics": [m.get("value") for m in row.get("metricValues", [])],
            }
            for row in data.get("rows", [])
        ]
