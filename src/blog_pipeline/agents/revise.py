"""SEO + GEO revision agent.

Runs only when the deterministic rubric (score_seo, which includes the GEO
levers — pull-quote, sources, chunk-sized sections) scores an article below
`SEO_MIN_SCORE`. It's handed the exact weaknesses (too short, keyword missing
from the intro, thin headings, low readability, missing pull-quote/sources,
sections outside the ~150-400 word chunk band, ...) and asked to revise the
HTML to fix them — without fabricating facts. The graph then re-scores the
revised body; the loop is capped at one pass so a stubbornly low-scoring
article still moves on rather than spinning.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from blog_pipeline.config import get_settings
from blog_pipeline.llm import CostTracker, structured_invoke
from blog_pipeline.schemas import RevisedDraft

SYSTEM = """You are an SEO/GEO editor. Revise the given article HTML to fix the \
listed weaknesses while keeping it accurate, natural, and on-topic.

Rules:
- Output clean semantic HTML only: <h2>, <h3>, <p>, <ul>/<li>, <ol>. No \
<html>/<head>/<body>/<h1>.
- Do NOT invent statistics, studies, or specific claims you can't stand \
behind. Add depth with defensible, general guidance instead — except for the \
pull_quote/sources fields below, which follow their own grounded rules.
- Preserve any existing <a href> internal links and <figure>/<img> blocks.
- Keep the reader's experience first — no keyword stuffing.
- If a weakness says sections aren't chunk-sized, split an oversized <h2> \
section into two, or expand a too-thin one — each <h2> section should stand \
alone (self-contained, no "as mentioned above") in roughly 150-400 words, \
opening with a direct 1-2 sentence answer before elaborating.
- If a weakness says the article is missing a pull-quote or sources: only set \
pull_quote if you have a genuinely quotable, honest one — first-party \
("Our team finds...") or naming a real, well-known standards body. Only add \
to sources real organizations you're confident exist and that are actually \
referenced in the revised body. Never fabricate a person, study, or \
organization. Leave pull_quote/sources empty/unset if nothing fits — a \
missing quote is better than an invented one."""


def _diagnose(metrics: dict, target_words: int, primary_keyword: str) -> list[str]:
    fixes: list[str] = []
    wc = metrics.get("word_count", 0)
    if wc and wc < 0.6 * target_words:
        fixes.append(
            f"Too short ({wc} words; aim for ~{target_words}). Expand sections "
            "with genuinely useful detail, examples, and practical steps."
        )
    if metrics.get("kw_in_title") is False:
        fixes.append(f"Work the primary keyword '{primary_keyword}' into the title.")
    if metrics.get("kw_in_intro") is False:
        fixes.append(
            f"Mention '{primary_keyword}' naturally within the first 100 words."
        )
    density = metrics.get("keyword_density", 0)
    if isinstance(density, (int, float)) and density < 0.003:
        fixes.append("Reference the primary keyword and close variants a bit more.")
    if metrics.get("h2_count", 0) < 2:
        fixes.append("Add clear <h2> section headings (at least 2-3).")
    flesch = metrics.get("flesch_reading_ease", 100)
    if isinstance(flesch, (int, float)) and flesch < 50:
        fixes.append(
            "Improve readability: shorter sentences and paragraphs, plainer words."
        )
    if metrics.get("internal_links", 0) < 2:
        fixes.append(
            "Weave in relevant internal links where anchor phrases already appear "
            "(don't invent URLs — only keep/adjust links already present)."
        )
    if metrics.get("has_pull_quote") is False:
        fixes.append(
            "Missing a pull_quote — add one genuinely quotable, grounded insight "
            "(see the pull_quote rule)."
        )
    if metrics.get("source_count", 0) < 1:
        fixes.append(
            "No named sources — if the article states an industry standard or "
            "best practice, name the real authoritative body in the text and add "
            "it to sources (see the sources rule)."
        )
    chunk = metrics.get("chunk_compliant_sections", "")
    if "/" in str(chunk):
        good, total = str(chunk).split("/", 1)
        if good.isdigit() and total.isdigit() and int(total) > 0 and int(good) < int(total):
            fixes.append(
                f"Only {good}/{total} sections are chunk-sized (~150-400 words, "
                "self-contained). Split oversized sections or expand thin ones so "
                "each <h2> stands alone in that band."
            )
    return fixes


def revise_article(
    *,
    body_html: str,
    primary_keyword: str,
    secondary_keywords: list[str],
    metrics: dict,
    pull_quote: str = "",
    sources: list[str] | None = None,
    cost: CostTracker | None = None,
) -> tuple[str, str, list[str]]:
    """Returns (body_html, pull_quote, sources) — pull_quote/sources pass
    through unchanged unless the revision explicitly improves them."""
    settings = get_settings()
    sources = sources or []
    fixes = _diagnose(metrics, settings.word_count_target, primary_keyword)
    if not fixes:
        # Nothing the rubric can name — leave everything unchanged.
        return body_html, pull_quote, sources

    human = [
        f"Primary keyword: {primary_keyword}",
        f"Secondary keywords: {', '.join(secondary_keywords)}"
        if secondary_keywords else "Secondary keywords: (none)",
        "Weaknesses to fix:\n" + "\n".join(f"- {f}" for f in fixes),
        f"Existing pull_quote: {pull_quote or '(none)'}",
        f"Existing sources: {', '.join(sources) if sources else '(none)'}",
        "Current article HTML:\n" + body_html,
    ]
    result: RevisedDraft = structured_invoke(
        model=settings.model_draft,
        schema=RevisedDraft,
        messages=[SystemMessage(content=SYSTEM), HumanMessage(content="\n\n".join(human))],
        temperature=0.6,
        stage="revise",
        cost=cost,
        max_tokens=16384,
    )
    new_body = result.body_html or body_html
    new_quote = result.pull_quote.strip() or pull_quote
    new_sources = result.sources or sources
    return new_body, new_quote, new_sources
