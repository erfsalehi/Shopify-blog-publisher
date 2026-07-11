"""Generative Engine Optimization (GEO / AI SEO).

Makes articles discoverable and citable by AI answer engines (ChatGPT, Claude,
Gemini, Google AI Overviews, Perplexity) — not just classic search. Two levers,
both applied to the article body deterministically (no extra LLM call):

  1. Answer-extractable *content*: a visible "Key takeaways" block near the top
     and a "Frequently asked questions" section — the self-contained Q&A and
     bullet facts that AI engines quote.
  2. *Structured data*: a JSON-LD `<script>` (schema.org Article + FAQPage) that
     crawlers parse to understand the page as data. FAQPage in particular is
     what powers rich results and AI answer citations.

The takeaways/FAQ come from the draft agent's structured output, so the visible
section and the JSON-LD are always in sync.
"""

from __future__ import annotations

import html
import json
import re

from blog_pipeline.config import get_settings
from blog_pipeline.schemas import FAQItem


def _esc(text: str) -> str:
    return html.escape((text or "").strip())


def render_takeaways(takeaways: list[str]) -> str:
    if not takeaways:
        return ""
    items = "".join(f"<li>{_esc(t)}</li>" for t in takeaways if t.strip())
    if not items:
        return ""
    return (
        '<div class="key-takeaways"><h2>Key takeaways</h2>'
        f"<ul>{items}</ul></div>"
    )


def render_faq(faq: list[FAQItem]) -> str:
    if not faq:
        return ""
    blocks = "".join(
        f"<h3>{_esc(f.question)}</h3><p>{_esc(f.answer)}</p>"
        for f in faq
        if f.question.strip() and f.answer.strip()
    )
    if not blocks:
        return ""
    return f'<section class="faq"><h2>Frequently asked questions</h2>{blocks}</section>'


def build_jsonld(
    *,
    title: str,
    description: str,
    faq: list[FAQItem],
    url: str | None = None,
) -> str:
    """schema.org Article + FAQPage as a single JSON-LD <script> block."""
    settings = get_settings()
    graph: list[dict] = []

    article: dict = {
        "@type": "Article",
        "headline": title,
        "description": description,
    }
    if url:
        article["url"] = url
        article["mainEntityOfPage"] = {"@type": "WebPage", "@id": url}
    if settings.business_name:
        publisher: dict = {"@type": "Organization", "name": settings.business_name}
        if settings.business_location:
            publisher["areaServed"] = settings.business_location
        article["publisher"] = publisher
        article["author"] = publisher
    graph.append(article)

    faq_entries = [
        {
            "@type": "Question",
            "name": f.question.strip(),
            "acceptedAnswer": {"@type": "Answer", "text": f.answer.strip()},
        }
        for f in faq
        if f.question.strip() and f.answer.strip()
    ]
    if faq_entries:
        graph.append({"@type": "FAQPage", "mainEntity": faq_entries})

    payload = {"@context": "https://schema.org", "@graph": graph}
    # </script> can't appear literally inside a <script> body.
    body = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return f'<script type="application/ld+json">{body}</script>'


def _inject_takeaways(body_html: str, takeaways_html: str) -> str:
    """Place the takeaways box right after the intro (before the first <h2>),
    or at the top if there's no heading yet."""
    if not takeaways_html:
        return body_html
    m = re.search(r"<h2", body_html, re.I)
    if m:
        return body_html[: m.start()] + takeaways_html + body_html[m.start():]
    return takeaways_html + body_html


def apply_geo(
    *,
    body_html: str,
    title: str,
    description: str,
    takeaways: list[str],
    faq: list[FAQItem],
    url: str | None = None,
) -> str:
    """Return body_html enriched with a takeaways box, a visible FAQ section,
    and JSON-LD structured data. Safe/idempotent-ish: only adds what's given."""
    if not get_settings().enable_geo:
        return body_html
    body_html = _inject_takeaways(body_html, render_takeaways(takeaways))
    body_html += render_faq(faq)
    body_html += build_jsonld(title=title, description=description, faq=faq, url=url)
    return body_html
