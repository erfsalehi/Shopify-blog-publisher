"""Competitor page scraper: extract H2/H3 headings from top-ranking URLs.

Feeds the outline agent so drafts cover the angles Google already rewards.
Best-effort and defensive: network/parse errors on any one URL are skipped,
never raised. Respects a short timeout and a browser-like User-Agent.
"""

from __future__ import annotations

import httpx
from bs4 import BeautifulSoup

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; BlogPipelineBot/0.1; +https://example.com/bot)"
    )
}


def extract_headers(url: str, timeout: float = 15.0) -> list[str]:
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
    except Exception:
        return []
    try:
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception:
        return []
    headers: list[str] = []
    for tag in soup.find_all(["h2", "h3"]):
        text = tag.get_text(strip=True)
        if text and 3 <= len(text) <= 120:
            headers.append(text)
    return headers


def gather_competitor_headers(urls: list[str], max_urls: int = 5) -> list[str]:
    """Collect and de-duplicate headings across the top competitor URLs."""
    seen: set[str] = set()
    collected: list[str] = []
    for url in urls[:max_urls]:
        for h in extract_headers(url):
            key = h.lower()
            if key not in seen:
                seen.add(key)
                collected.append(h)
    return collected
