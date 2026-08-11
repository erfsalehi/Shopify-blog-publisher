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
        # Why the last call returned nothing. Every method still degrades to
        # [] so a missing key can't fail the weekly refresh, but "no data" and
        # "your account is unverified" are different problems and a caller
        # that can only see [] cannot tell the owner which one it hit.
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.login and self.password)

    def _auth_header(self) -> dict:
        token = base64.b64encode(f"{self.login}:{self.password}".encode()).decode()
        return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    def _post(self, path: str, payload: list) -> dict | None:
        self.last_error = None
        if not self.enabled:
            self.last_error = "DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD not set"
            return None
        try:
            resp = httpx.post(
                f"{BASE}{path}", headers=self._auth_header(), json=payload, timeout=60.0
            )
            data = resp.json() if resp.content else {}
            # DataForSEO reports failures in the body with HTTP 200 as often
            # as with a 4xx, so the status code alone is not the answer.
            code = data.get("status_code")
            if code and code != 20000:
                self.last_error = f"{code}: {data.get('status_message')}"
                return None
            resp.raise_for_status()
            # The envelope succeeding (20000, "Ok.") only means the request
            # was accepted — it says nothing about whether the task inside it
            # ran. A single malformed keyword (DataForSEO rejects certain
            # punctuation, e.g. "?") fails the whole task with its own
            # status_code while the envelope still reports 20000, so a caller
            # that only checked the top level would see success with an empty
            # result and no explanation. Surface the task's own message
            # instead of leaving that silent.
            tasks = data.get("tasks") or []
            if tasks and all(t.get("status_code") != 20000 for t in tasks):
                failed = tasks[0]
                self.last_error = (
                    f"{failed.get('status_code')}: {failed.get('status_message')}"
                )
                return None
            return data
        except Exception as e:
            if not self.last_error:
                self.last_error = f"{type(e).__name__}: {e}"
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

    def local_serp(
        self,
        keyword: str,
        *,
        location_code: int,
        language_code: str = "en",
        depth: int = 20,
    ) -> dict | None:
        """One SERP as Google shows it *inside* a given city.

        This is the only way to answer "where do we rank in Langley". Search
        Console reports a single national average position, which for a
        business with one showroom is close to meaningless — "flooring
        langley" at position 16 nationally says nothing about position 16 in
        Langley, and Langley is the only place the answer matters.

        `advanced` rather than `regular` because it returns the local pack
        (`local_pack` items) alongside the organic list. For a flooring
        showroom the pack is usually the thing that produces the phone call,
        and it's a separate competition from the blue links — you can be
        absent from one and first in the other.

        Returns the raw first result, or None with `last_error` set.
        """
        data = self._post(
            "/serp/google/organic/live/advanced",
            [{
                "keyword": keyword,
                "location_code": location_code,
                "language_code": language_code,
                "depth": depth,
                # Ask Google for the mobile SERP: local intent is
                # overwhelmingly phone traffic, and the pack sits higher there.
                "device": "mobile",
            }],
        )
        if not data:
            return None
        tasks = data.get("tasks") or []
        results = (tasks[0].get("result") or []) if tasks else []
        return results[0] if results else None
