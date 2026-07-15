"""Content refresh agent — brings an existing live post back up to date.

Distinct from agents/revise.py, which cannot do this job: revise is driven by
_diagnose(metrics) and returns the body untouched when the SEO rubric has no
complaint. A four-year-old article can score perfectly on that rubric while
being thoroughly stale — right keyword density, wrong decade. Staleness isn't
a rubric failure, so it needs its own prompt and its own reasons to edit.

The output overwrites a live, indexed page (Shopify has no draft revision for
a published post), which shapes the prompt heavily: preserve what already
earns rankings, never rename or restructure for its own sake, and prefer an
honest no-op over invented churn. The caller snapshots the previous body
first — see db.ArticleRevision.
"""

from __future__ import annotations

from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, SystemMessage

from blog_pipeline.config import get_settings
from blog_pipeline.llm import CostTracker, structured_invoke
from blog_pipeline.schemas import RefreshedArticle

SYSTEM = """You are a content editor refreshing an article that is already \
published and may already rank in Google. Your edits go live immediately.

That asymmetry governs everything: a page that ranks has earned something you \
cannot see from the text alone. Improving it is worth real money; breaking it \
costs real money. When a change is marginal, don't make it.

Rules:
- Output the COMPLETE refreshed article as clean semantic HTML: <h2>, <h3>, \
<p>, <ul>/<ol>/<li>, <blockquote>. No <html>/<head>/<body>/<h1>.
- PRESERVE every existing <a href> link and <figure>/<img> block exactly. They \
are internal links and hosted assets — dropping one is a real regression.
- Do NOT invent statistics, prices, studies, dates, or product claims. You do \
not know today's prices or this year's model numbers. If something reads as \
dated but you can't source the current fact, rewrite it to be durable \
("modern vinyl planks typically...") rather than swapping in a guess. A \
plausible invented number is the worst possible outcome here.
- Keep the article's angle and scope. This is a refresh, not a rewrite: the \
reader who searched for this should still land on what they wanted.
- Prefer keeping the title. A ranking page's title is load-bearing; only \
propose a new one if the current is genuinely poor.

What actually justifies an edit:
- Advice that has aged badly, or omits an option now standard in the field.
- Thin sections that don't answer the question they promise.
- Structure: sections that ramble past ~400 words or stop under ~150, where \
each <h2> should stand alone and open with a direct answer.
- Missing depth a reader would now expect — practical steps, comparisons, \
trade-offs, common mistakes.
- Readability: shorter sentences, plainer words, less throat-clearing.

If none of that applies, set skipped=true and return the body unchanged. An \
honest no-op is a good answer; busywork on a ranking page is not."""


def _age_hint(published_at: datetime | None) -> str:
    if published_at is None:
        return "Published: unknown."
    now = datetime.now(timezone.utc)
    # Imported rows may be naive depending on the backend that stored them.
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    years = (now - published_at).days / 365.25
    return (
        f"Published: {published_at.date().isoformat()} "
        f"(~{years:.1f} years ago). Judge what has plausibly changed in the "
        "field since then — but do not invent specifics to fill the gap."
    )


def refresh_article(
    *,
    title: str,
    body_html: str,
    published_at: datetime | None = None,
    business_context: str = "",
    cost: CostTracker | None = None,
) -> RefreshedArticle:
    """Refresh one live post. Returns the article unchanged with skipped=True
    when the model judges it doesn't need the work."""
    settings = get_settings()

    human = [
        f"Title: {title}",
        _age_hint(published_at),
        f"Target length: roughly {settings.word_count_target} words.",
    ]
    if business_context:
        human.append(f"Publisher context: {business_context}")
    human.append("Current article HTML:\n" + body_html)

    result: RefreshedArticle = structured_invoke(
        model=settings.model_draft,
        schema=RefreshedArticle,
        messages=[
            SystemMessage(content=SYSTEM),
            HumanMessage(content="\n\n".join(human)),
        ],
        temperature=0.5,
        stage="refresh",
        cost=cost,
        max_tokens=16384,
    )
    # A model can set skipped and still return a mangled/empty body; trust the
    # flag, not the payload, and hand back exactly what was there before.
    if result.skipped or not result.body_html.strip():
        return RefreshedArticle(
            body_html=body_html, change_summary=[], skipped=True
        )
    return result
