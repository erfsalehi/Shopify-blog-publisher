"""Google Search Console client — what the site actually ranks for.

DataForSEO tells you what the market searches; this tells you what *you* get
shown for and where you sit. That makes it the only source here that can rank
existing pages by decay, which is what the refresh agent needs, and the only
one that can spot a query you already earn impressions for but don't win.

Auth is a service account: its key JSON goes in one secret, and the account's
email must be added as a user on the property in Search Console itself —
creating the key is not enough, and skipping that step is the usual first
failure (it presents as a 403 on a property you can plainly see in the UI).

Like DataForSEOClient, every method degrades to [] rather than raising when
unconfigured, so the weekly refresh never hard-fails on a missing key. Import
of google-auth is deferred into _token() so the package stays optional.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from blog_pipeline.config import get_settings

API = "https://searchconsole.googleapis.com/webmasters/v3"
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
# GSC's own cap per request. Paging beyond it uses startRow.
_MAX_ROWS = 25000


class SearchConsoleError(RuntimeError):
    pass


class SearchConsoleClient:
    def __init__(self, credentials_json: str | None = None, site_url: str | None = None):
        s = get_settings()
        self._credentials_json = (
            credentials_json if credentials_json is not None else s.gsc_credentials_json
        )
        self.site_url = site_url or s.gsc_property
        self._cached_token: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._credentials_json and self.site_url)

    def _token(self) -> str:
        if self._cached_token:
            return self._cached_token
        try:
            import json

            from google.auth.transport.requests import Request
            from google.oauth2 import service_account
        except ImportError as e:  # pragma: no cover - depends on the extra
            raise SearchConsoleError(
                "Search Console needs the [gsc] extra: pip install -e '.[gsc]'"
            ) from e

        try:
            info = json.loads(self._credentials_json)
        except ValueError as e:
            raise SearchConsoleError(
                "GSC_CREDENTIALS_JSON is not valid JSON — paste the service "
                "account key file whole, including the outer braces."
            ) from e

        try:
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=[SCOPE]
            )
            creds.refresh(Request())
        except Exception as e:
            raise SearchConsoleError(f"Could not authenticate to Google: {e}") from e
        self._cached_token = creds.token
        return self._cached_token

    def _post(self, path: str, payload: dict) -> dict:
        resp = httpx.post(
            f"{API}{path}",
            headers={"Authorization": f"Bearer {self._token()}"},
            json=payload,
            timeout=60.0,
        )
        if resp.status_code == 403:
            raise SearchConsoleError(
                f"403 for {self.site_url}. The service account almost certainly "
                "isn't a user on this property yet — add its client_email in "
                "Search Console → Settings → Users and permissions. "
                "`blog-pipeline sync-performance --list-sites` shows what it can see."
            )
        if resp.status_code == 404:
            raise SearchConsoleError(
                f"404 for {self.site_url}. The property string must match Search "
                "Console exactly: domain properties are 'sc-domain:example.com', "
                "URL-prefix ones are 'https://example.com/' with the slash. "
                "Try --list-sites."
            )
        resp.raise_for_status()
        return resp.json()

    def list_sites(self) -> list[dict]:
        """Properties this service account can read. The setup diagnostic:
        an empty list means the key works but was never granted access."""
        if not self._credentials_json:
            return []
        resp = httpx.get(
            f"{API}/sites",
            headers={"Authorization": f"Bearer {self._token()}"},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json().get("siteEntry", [])

    def query(
        self,
        *,
        dimensions: list[str],
        start_date: date,
        end_date: date,
        row_limit: int = _MAX_ROWS,
    ) -> list[dict]:
        """searchAnalytics rows as {keys: [...], clicks, impressions, ctr,
        position}. Pages through startRow so a big site isn't silently cut off
        at the first 25k."""
        if not self.enabled:
            return []
        path = f"/sites/{_quote(self.site_url)}/searchAnalytics/query"
        rows: list[dict] = []
        start_row = 0
        while len(rows) < row_limit:
            page_size = min(_MAX_ROWS, row_limit - len(rows))
            data = self._post(
                path,
                {
                    "startDate": start_date.isoformat(),
                    "endDate": end_date.isoformat(),
                    "dimensions": dimensions,
                    "rowLimit": page_size,
                    "startRow": start_row,
                },
            )
            page = data.get("rows", [])
            rows.extend(page)
            if len(page) < page_size:
                break
            start_row += len(page)
        return rows


def _quote(value: str) -> str:
    from urllib.parse import quote

    # sc-domain:example.com contains a colon, which must be escaped in the path.
    return quote(value, safe="")


def default_window(days: int = 90) -> tuple[date, date]:
    """Search Console lags ~2-3 days; ending today would report a partial tail
    as a decline. End 3 days back instead."""
    end = date.today() - timedelta(days=3)
    return end - timedelta(days=days), end
