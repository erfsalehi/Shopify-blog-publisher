"""DataForSEO client (Standard Queue) for keyword volume/difficulty + SERP.

Only the two endpoints the pipeline needs are wrapped. Auth is HTTP Basic with
the account login/password. Every method degrades to [] on error or when
credentials are absent, so the topic-research agent can fall back to LLM-only
reasoning rather than failing the weekly refresh.
"""

from __future__ import annotations

import base64

import httpx

from blog_pipeline.config import get_settings

BASE = "https://api.dataforseo.com/v3"


class DataForSEOClient:
    def __init__(self, login: str | None = None, password: str | None = None) -> None:
        s = get_settings()
        self.login = login or s.dataforseo_login
        self.password = password or s.dataforseo_password

    @property
    def enabled(self) -> bool:
        return bool(self.login and self.password)

    def _auth_header(self) -> dict:
        token = base64.b64encode(f"{self.login}:{self.password}".encode()).decode()
        return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    def _post(self, path: str, payload: list) -> dict | None:
        if not self.enabled:
            return None
        try:
            resp = httpx.post(
                f"{BASE}{path}", headers=self._auth_header(), json=payload, timeout=60.0
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    def keyword_data(
        self, keywords: list[str], location_code: int = 2840, language_code: str = "en"
    ) -> list[dict]:
        """Search volume + competition for keywords. location 2840 = US."""
        data = self._post(
            "/keywords_data/google_ads/search_volume/live",
            [{"keywords": keywords, "location_code": location_code,
              "language_code": language_code}],
        )
        if not data:
            return []
        out: list[dict] = []
        for task in data.get("tasks", []):
            for item in task.get("result", []) or []:
                out.append(
                    {
                        "keyword": item.get("keyword"),
                        "search_volume": item.get("search_volume"),
                        "competition": item.get("competition_index"),
                        "cpc": item.get("cpc"),
                    }
                )
        return out

    def serp_top(
        self, keyword: str, location_code: int = 2840, language_code: str = "en",
        depth: int = 10,
    ) -> list[dict]:
        """Top organic results for a keyword: [{title, url, description}]."""
        data = self._post(
            "/serp/google/organic/live/regular",
            [{"keyword": keyword, "location_code": location_code,
              "language_code": language_code, "depth": depth}],
        )
        if not data:
            return []
        results: list[dict] = []
        for task in data.get("tasks", []):
            for res in task.get("result", []) or []:
                for item in res.get("items", []) or []:
                    if item.get("type") == "organic":
                        results.append(
                            {
                                "title": item.get("title"),
                                "url": item.get("url"),
                                "description": item.get("description"),
                            }
                        )
        return results
