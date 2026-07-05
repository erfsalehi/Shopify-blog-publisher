"""SEO optimization stage (Haiku + deterministic checks).

Two parts:
  1. Deterministic scoring/analysis (no LLM): keyword presence/density,
     Flesch readability (textstat), heading/length checks -> rubric score /100.
  2. Internal link insertion: match published articles / catalog anchors to
     phrases in the body and hyperlink the first occurrence of each.
  3. LLM polish of seo.title / seo.description for the Shopify SEO fields.

Keeping scoring deterministic makes it unit-testable and keeps the rubric
stable across runs (the PRD's >=85 target needs a reproducible measure).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import textstat
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from blog_pipeline.config import get_settings
from blog_pipeline.llm import CostTracker, make_llm
from blog_pipeline.utils import html_to_text, word_count


@dataclass
class SEOResult:
    score: float
    seo_title: str
    seo_description: str
    body_html: str
    metrics: dict = field(default_factory=dict)
    internal_links_added: int = 0


class _SeoMeta(BaseModel):
    seo_title: str
    seo_description: str


def _keyword_density(text: str, keyword: str) -> float:
    if not keyword:
        return 0.0
    words = text.lower().split()
    if not words:
        return 0.0
    kw = keyword.lower()
    hits = text.lower().count(kw)
    # occurrences of the phrase, weighted by its word length, over total words
    return (hits * len(kw.split())) / len(words)


def score_seo(
    body_html: str,
    title: str,
    meta_description: str,
    primary_keyword: str,
    secondary_keywords: list[str],
) -> tuple[float, dict]:
    """Deterministic on-page rubric -> (score 0-100, metrics dict)."""
    text = html_to_text(body_html)
    wc = word_count(body_html)
    target = get_settings().word_count_target

    metrics: dict = {}
    score = 0.0

    # Length (20): within 60-140% of target.
    ratio = wc / target if target else 1.0
    length_ok = 0.6 <= ratio <= 1.4
    metrics["word_count"] = wc
    score += 20 if length_ok else max(0, 20 - abs(1 - ratio) * 20)

    # Primary keyword in title (15) + first 100 words (10).
    metrics["kw_in_title"] = primary_keyword.lower() in title.lower()
    score += 15 if metrics["kw_in_title"] else 0
    intro = " ".join(text.split()[:100]).lower()
    metrics["kw_in_intro"] = primary_keyword.lower() in intro
    score += 10 if metrics["kw_in_intro"] else 0

    # Primary keyword density in a healthy 0.3%-2.5% band (15).
    density = _keyword_density(text, primary_keyword)
    metrics["keyword_density"] = round(density, 4)
    score += 15 if 0.003 <= density <= 0.025 else (7 if density > 0 else 0)

    # Secondary keyword coverage (10).
    if secondary_keywords:
        covered = sum(1 for k in secondary_keywords if k.lower() in text.lower())
        frac = covered / len(secondary_keywords)
        metrics["secondary_coverage"] = round(frac, 2)
        score += 10 * frac
    else:
        score += 10

    # Headings present (10): at least 2 H2s.
    h2_count = len(re.findall(r"<h2", body_html, re.I))
    metrics["h2_count"] = h2_count
    score += 10 if h2_count >= 2 else h2_count * 5

    # Readability (10): Flesch reading ease >= 50 is "fairly readable".
    try:
        flesch = textstat.flesch_reading_ease(text) if text else 0
    except Exception:
        flesch = 0
    metrics["flesch_reading_ease"] = round(flesch, 1)
    score += 10 if flesch >= 50 else max(0, flesch / 5)

    # Meta description length 120-160 (10).
    md_len = len(meta_description or "")
    metrics["meta_description_length"] = md_len
    score += 10 if 120 <= md_len <= 160 else (5 if 80 <= md_len <= 200 else 0)

    return round(min(score, 100.0), 1), metrics


def insert_internal_links(
    body_html: str, targets: list[dict], max_links: int = 4
) -> tuple[str, int]:
    """Hyperlink the first occurrence of each target's title in the body.

    Skips text already inside a tag or an existing anchor (naive: only links
    inside <p>...</p> plain runs). Returns (html, links_added).
    """
    added = 0
    result = body_html
    for target in targets:
        if added >= max_links:
            break
        anchor = target.get("title", "").strip()
        url = target.get("url", "").strip()
        if not anchor or not url or len(anchor) < 4:
            continue
        # Only replace when the phrase appears as visible text (rough guard:
        # not immediately preceded by '>' of an anchor or inside a tag).
        pattern = re.compile(
            r"(?<![\">])\b" + re.escape(anchor) + r"\b(?![^<]*</a>)", re.I
        )
        new_result, n = pattern.subn(
            f'<a href="{url}">{anchor}</a>', result, count=1
        )
        if n:
            result = new_result
            added += 1
    return result, added


def optimize_seo(
    *,
    body_html: str,
    title: str,
    meta_description: str,
    primary_keyword: str,
    secondary_keywords: list[str],
    link_targets: list[dict] | None = None,
    cost: CostTracker | None = None,
) -> SEOResult:
    settings = get_settings()

    body, n_links = insert_internal_links(body_html, link_targets or [])

    # LLM polish of the SEO meta fields.
    seo_title, seo_description = title, meta_description
    try:
        llm = make_llm(settings.model_seo, temperature=0.3)
        structured = llm.with_structured_output(_SeoMeta, include_raw=True)
        res = structured.invoke(
            [
                SystemMessage(
                    content="You optimize on-page SEO meta fields. Return a "
                    "compelling <=60 char SEO title including the primary keyword, "
                    "and a 150-160 char meta description with a call to read."
                ),
                HumanMessage(
                    content=f"Primary keyword: {primary_keyword}\n"
                    f"Article title: {title}\n"
                    f"Current meta: {meta_description}"
                ),
            ]
        )
        if cost is not None:
            cost.record("seo", settings.model_seo, res["raw"])
        meta = res["parsed"]
        seo_title, seo_description = meta.seo_title, meta.seo_description
    except Exception:
        # SEO meta polish is best-effort; keep draft values on failure.
        pass

    score, metrics = score_seo(
        body, title, seo_description, primary_keyword, secondary_keywords
    )
    return SEOResult(
        score=score,
        seo_title=seo_title,
        seo_description=seo_description,
        body_html=body,
        metrics=metrics,
        internal_links_added=n_links,
    )
