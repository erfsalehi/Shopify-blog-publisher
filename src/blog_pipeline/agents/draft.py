"""Draft agent (Sonnet — the one reader-visible quality step).

Takes the outline + brand voice guide and produces the full article: title,
meta description, semantic HTML body, and image slot requests with alt text.
"""

from __future__ import annotations

import re

from langchain_core.messages import HumanMessage, SystemMessage

from blog_pipeline.config import get_settings
from blog_pipeline.llm import CostTracker, structured_invoke
from blog_pipeline.prompts import brand_voice
from blog_pipeline.schemas import Draft, Outline

SYSTEM = """You are an expert blog writer. Write a complete, original, factually \
careful article from the given outline. Follow the brand voice guide exactly.

Rules:
- Output the body as clean semantic HTML: <h2>, <h3>, <p>, <ul>/<li>, <ol>.
  Do NOT include <html>, <head>, <body>, or an <h1> (the title is separate).
- Do NOT add your own hyperlinks or <a> tags (no href="#" placeholders).
  Internal links are added in a later stage.
- Write the COMPLETE article, all the way through a final concluding paragraph.
  Never stop mid-section or mid-sentence. The body must end with a properly
  closed HTML tag (e.g. </p>).
- Do NOT invent statistics, studies, quotes, or specific claims you can't stand
  behind. Prefer general, defensible statements — except where the grounded
  guidance below explicitly asks for real, verifiable specifics.
- Propose 2-3 image slots: one 'featured' plus inline images at natural breaks.
  Each needs a vivid text-to-image prompt and concise SEO alt text.
- Write a 150-160 character meta description.

GENERATIVE ENGINE OPTIMIZATION (GEO) — write so AI answer engines (ChatGPT,
Claude, Gemini, Google AI Overviews, Perplexity) retrieve and cite this page,
not just so it ranks in classic search. This follows the peer-reviewed
findings of Aggarwal et al., "GEO: Generative Engine Optimization" (KDD 2024,
Princeton/Georgia Tech/IIT Delhi/Allen Institute for AI), which measured what
actually increases AI citation rates:

1. Chunking — AI retrieval systems (RAG) don't read the whole page; they
   split it into independent ~150-400 word chunks and retrieve whichever
   chunk best answers the query. So EVERY <h2> section must:
     - Be self-contained: understandable with NO context from other sections.
       Never write "as mentioned above/below" or rely on an earlier section.
     - Open with a 1-2 sentence direct-answer "capsule summary" of that
       section's question, THEN elaborate.
     - Land roughly 150-400 words — split an oversized section into two
       H2s/H3s rather than writing one long section.
2. Statistics (+32% citation rate in the study) — where you have a real,
   defensible, well-established number (industry-standard measurements,
   typical ranges, widely known figures), state it specifically instead of
   vaguely. Never fabricate a specific statistic or attribute one to a study
   that doesn't exist — an invented number is worse than none.
3. Quotations (+41%, the single biggest lever) — include exactly one short,
   genuinely quotable `pull_quote`: either the publishing business's own
   expert insight (first-party, honest — "Our installers find...") or a real,
   well-known standards body's guidance named plainly. Never a fabricated
   named individual or invented study.
4. Citing sources (+30%) — when you state an industry standard, certification,
   or best practice, name the real authoritative body (e.g. National Wood
   Flooring Association / NWFA, ANSI, ASTM, EPA) inline in the prose, and list
   every one you named in `sources`. Only real organizations you're confident
   exist — never invent one.

Also, beyond the study's findings:
- Open the article with a direct, self-contained 2-3 sentence answer to its
  core question before any preamble.
- Phrase H2 headings as the real questions people ask where natural.
- Provide 3-5 key_takeaways: crisp, standalone factual sentences.
- Provide 3-6 faq pairs of genuinely common questions with direct answers.
  (These become a visible FAQ section AND FAQPage structured data.)"""


def _looks_truncated(body_html: str) -> bool:
    """Heuristic: a complete article body ends on a closed HTML tag. If the
    last non-whitespace character isn't '>', the model very likely stopped
    mid-sentence and the draft is incomplete."""
    stripped = (body_html or "").rstrip()
    return not stripped.endswith(">")


_ANCHOR = re.compile(r'<a\b[^>]*?\bhref\s*=\s*(["\'])(.*?)\1[^>]*>(.*?)</a>', re.I | re.S)


def _strip_bad_links(html: str) -> str:
    """Unwrap any anchors the model invented that don't point at a real URL
    (href="#", empty, or relative) — keep the text, drop the broken link. Real
    internal links (added later, pointing at http(s) catalog URLs) are kept."""

    def _repl(m: re.Match) -> str:
        href = m.group(2).strip().lower()
        return m.group(0) if href.startswith(("http://", "https://")) else m.group(3)

    return _ANCHOR.sub(_repl, html or "")


def _outline_to_text(outline: Outline) -> str:
    lines = [f"Working title: {outline.working_title}"]
    lines.append(f"Primary keyword: {outline.primary_keyword}")
    if outline.secondary_keywords:
        lines.append(f"Secondary keywords: {', '.join(outline.secondary_keywords)}")
    lines.append("Sections:")
    for s in outline.sections:
        lines.append(f"  ## {s.heading}")
        for sp in s.subpoints:
            lines.append(f"     - {sp}")
    return "\n".join(lines)


def generate_draft(
    outline: Outline,
    competitor_headers: list[str] | None = None,
    cost: CostTracker | None = None,
) -> Draft:
    settings = get_settings()

    voice = brand_voice()
    human = [
        "BRAND VOICE GUIDE:\n" + (voice or "(no guide provided — use a clear, "
        "helpful, professional tone)"),
        "OUTLINE:\n" + _outline_to_text(outline),
        f"Target length: ~{settings.word_count_target} words.",
    ]
    if competitor_headers:
        joined = "\n".join(f"- {h}" for h in competitor_headers[:40])
        human.append(
            "TOP-RANKING COMPETITOR SUBHEADINGS for this keyword (from live "
            "SERP results). Cover the substantive angles these address so the "
            "piece is competitive and specific — but write original copy, don't "
            "copy their wording, and add depth or a gap they miss:\n" + joined
        )

    from blog_pipeline.agents.promote import draft_shop_hint

    shop_hint = draft_shop_hint()
    if shop_hint:
        human.append(shop_hint)

    messages = [SystemMessage(content=SYSTEM), HumanMessage(content="\n\n".join(human))]

    # The model occasionally ends the body early. Regenerate once if the draft
    # comes back looking truncated — QA rejects truncated bodies, so catching
    # it here keeps otherwise-good articles on the auto-publish path.
    draft: Draft | None = None
    for _ in range(2):
        draft = structured_invoke(
            model=settings.model_draft,
            schema=Draft,
            messages=messages,
            temperature=0.7,
            stage="draft",
            cost=cost,
            # Full HTML body lives in one JSON field — give it room.
            max_tokens=16384,
        )
        if not _looks_truncated(draft.body_html):
            break
    draft.body_html = _strip_bad_links(draft.body_html)
    return draft
