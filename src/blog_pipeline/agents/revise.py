"""SEO revision agent.

Runs only when the deterministic SEO rubric scores an article below the
`SEO_MIN_SCORE` pass mark. It's handed the exact rubric weaknesses (too short,
keyword missing from the intro, thin headings, low readability, ...) and asked
to revise the HTML to fix them — without fabricating facts. The graph then
re-scores the revised body; the loop is capped at one pass so a stubbornly
low-scoring article still moves on rather than spinning.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from blog_pipeline.config import get_settings
from blog_pipeline.llm import CostTracker, structured_invoke
from blog_pipeline.schemas import RevisedDraft

SYSTEM = """You are an SEO editor. Revise the given article HTML to fix the \
listed on-page weaknesses while keeping it accurate, natural, and on-topic.

Rules:
- Output clean semantic HTML only: <h2>, <h3>, <p>, <ul>/<li>, <ol>. No \
<html>/<head>/<body>/<h1>.
- Do NOT invent statistics, studies, quotes, or specific claims. Add depth \
with defensible, general guidance instead.
- Preserve any existing <a href> internal links and <figure>/<img> blocks.
- Keep the reader's experience first — no keyword stuffing."""


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
    return fixes


def revise_article(
    *,
    body_html: str,
    primary_keyword: str,
    secondary_keywords: list[str],
    metrics: dict,
    cost: CostTracker | None = None,
) -> str:
    settings = get_settings()
    fixes = _diagnose(metrics, settings.word_count_target, primary_keyword)
    if not fixes:
        # Nothing the rubric can name — leave the body unchanged.
        return body_html

    human = [
        f"Primary keyword: {primary_keyword}",
        f"Secondary keywords: {', '.join(secondary_keywords)}"
        if secondary_keywords else "Secondary keywords: (none)",
        "Weaknesses to fix:\n" + "\n".join(f"- {f}" for f in fixes),
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
    return result.body_html or body_html
