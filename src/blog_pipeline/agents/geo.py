"""Generative Engine Optimization (GEO / AI SEO).

Makes articles discoverable and citable by AI answer engines (ChatGPT, Claude,
Gemini, Google AI Overviews, Perplexity) — not just classic search. Implements
the levers from Aggarwal et al., "GEO: Generative Engine Optimization"
(KDD 2024), applied to the article body deterministically (no extra LLM call
beyond what the draft agent already produced):

  1. Answer-extractable *content*: a visible "Key takeaways" block near the
     top and a "Frequently asked questions" section — self-contained Q&A and
     bullet facts sized for how AI retrieval chunks a page (~150-400 words),
     each independently understandable without the rest of the article.
  2. A quotable **pull-quote** (the study's single biggest lever, +41%
     citation rate) — rendered as a semantic <blockquote>.
  3. Named **sources** (+30%) — the real standards bodies/organizations the
     draft cited, rendered as a visible list and echoed into JSON-LD.
  4. *Structured data*: a JSON-LD `<script>` (schema.org Article + FAQPage)
     that crawlers parse to understand the page as data. FAQPage in
     particular is what powers rich results and AI answer citations.

The takeaways/FAQ/quote/sources all come from the draft agent's structured
output, so the visible sections and the JSON-LD are always in sync.
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


def render_pull_quote(pull_quote: str) -> str:
    """A quotable insight as a semantic <blockquote> — GEO's single biggest
    lever (Aggarwal et al. 2024: +41% AI citation rate for quotations)."""
    q = _esc(pull_quote)
    if not q:
        return ""
    return f'<blockquote class="pull-quote"><p>{q}</p></blockquote>'


def render_sources(sources: list[str]) -> str:
    """Visible list of the real standards bodies/organizations the article
    cited (Aggarwal et al. 2024: +30% AI citation rate for cited sources)."""
    names = [s.strip() for s in sources if s.strip()]
    if not names:
        return ""
    items = "".join(f"<li>{_esc(n)}</li>" for n in names)
    return (
        '<section class="sources"><h2>Sources &amp; standards referenced</h2>'
        f"<ul>{items}</ul></section>"
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
    sources: list[str] | None = None,
) -> str:
    """schema.org Article + LocalBusiness + BreadcrumbList + FAQPage as a single JSON-LD block."""
    settings = get_settings()
    graph: list[dict] = []
    base_url = settings.store_link_base or "https://drflooring.ca"
    org_id = f"{base_url}/#organization"

    # 1. LocalBusiness / FlooringStore schema for D&R Flooring
    biz_name = settings.business_name or "D&R Flooring"
    local_business: dict = {
        "@type": "LocalBusiness",
        "@id": org_id,
        "name": biz_name,
        "url": base_url,
        "telephone": settings.business_phone or "+1-604-532-2211",
        "priceRange": "$$",
        "address": {
            "@type": "PostalAddress",
            "streetAddress": "#103 - 20551 Langley Bypass",
            "addressLocality": "Langley",
            "addressRegion": "BC",
            "postalCode": "V3A 5E8",
            "addressCountry": "CA",
        },
        "geo": {
            "@type": "GeoCoordinates",
            "latitude": 49.1147,
            "longitude": -122.6565,
        },
        "areaServed": [
            {"@type": "AdministrativeArea", "name": "Langley"},
            {"@type": "AdministrativeArea", "name": "Surrey"},
            {"@type": "AdministrativeArea", "name": "Abbotsford"},
            {"@type": "AdministrativeArea", "name": "White Rock"},
            {"@type": "AdministrativeArea", "name": "Fraser Valley"},
            {"@type": "AdministrativeArea", "name": "Lower Mainland"},
            {"@type": "AdministrativeArea", "name": "British Columbia"},
        ],
    }
    if settings.business_hours:
        local_business["openingHours"] = settings.business_hours
    graph.append(local_business)

    # 2. Article schema
    clean_title = re.sub(r"<[^>]+>", "", title).strip()
    clean_desc = re.sub(r"<[^>]+>", "", description).strip()
    article: dict = {
        "@type": "Article",
        "headline": clean_title,
        "description": clean_desc,
    }
    if url:
        article["url"] = url
        article["mainEntityOfPage"] = {"@type": "WebPage", "@id": url}
    publisher_ref = {"@type": "Organization", "name": biz_name, "@id": org_id}
    if settings.business_location:
        publisher_ref["areaServed"] = settings.business_location
    article["publisher"] = publisher_ref
    article["author"] = publisher_ref
    names = [s.strip() for s in (sources or []) if s.strip()]
    if names:
        article["citation"] = names
    graph.append(article)

    # 3. BreadcrumbList schema
    if url:
        graph.append({
            "@type": "BreadcrumbList",
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": 1,
                    "name": "Home",
                    "item": base_url,
                },
                {
                    "@type": "ListItem",
                    "position": 2,
                    "name": "Blog",
                    "item": f"{base_url}/blogs/news",
                },
                {
                    "@type": "ListItem",
                    "position": 3,
                    "name": clean_title,
                    "item": url,
                },
            ],
        })

    # 4. FAQPage schema
    faq_entries = [
        {
            "@type": "Question",
            "name": re.sub(r"<[^>]+>", "", f.question).strip(),
            "acceptedAnswer": {"@type": "Answer", "text": re.sub(r"<[^>]+>", "", f.answer).strip()},
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


def _inject_after_intro(body_html: str, block_html: str) -> str:
    """Place a block right after the intro (before the first <h2>), or at the
    top if there's no heading yet."""
    if not block_html:
        return body_html
    m = re.search(r"<h2", body_html, re.I)
    if m:
        return body_html[: m.start()] + block_html + body_html[m.start():]
    return block_html + body_html


def apply_geo(
    *,
    body_html: str,
    title: str,
    description: str,
    takeaways: list[str],
    faq: list[FAQItem],
    pull_quote: str = "",
    sources: list[str] | None = None,
    url: str | None = None,
) -> str:
    """Return body_html enriched with a takeaways box + pull-quote near the
    top, a sources list + visible FAQ section near the end, and JSON-LD
    structured data. Safe/idempotent-ish: only adds what's given."""
    if not get_settings().enable_geo:
        return body_html
    sources = sources or []
    lead_block = render_takeaways(takeaways) + render_pull_quote(pull_quote)
    body_html = _inject_after_intro(body_html, lead_block)
    body_html += render_sources(sources)
    body_html += render_faq(faq)
    body_html += build_jsonld(
        title=title, description=description, faq=faq, url=url, sources=sources
    )
    return body_html
