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
    names = [s.strip() for s in (sources or []) if s.strip()]
    if names:
        # citation as plain org names is the pragmatic choice here — we don't
        # have verifiable URLs for each standards body, and inventing one
        # would be worse than a name-only citation.
        article["citation"] = names
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
