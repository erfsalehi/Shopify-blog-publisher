"""QA / guardrail stage (Opus — last line of defense before publish).

Runs a single structured reasoning pass that:
  * flags unverifiable factual claims,
  * checks brand safety (tone + configured banned topics/claims),
  * notes if the piece duplicates one of the store's existing titles,
  * returns an overall publish confidence 0-1 and a verdict.

The confidence + verdict drive routing in the graph (auto-publish vs. human
gate vs. block). Banned-topic matching is also enforced deterministically so a
hard block can't be talked out of by the model.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from blog_pipeline.config import get_settings
from blog_pipeline.llm import CostTracker, structured_invoke
from blog_pipeline.schemas import QAReport
from blog_pipeline.utils import html_to_text

SYSTEM = """You are a meticulous content QA reviewer and fact-checker. Review the \
article and return a structured assessment.

Assess:
- unverifiable_claims: specific factual assertions (stats, dates, studies, \
quotes, superlatives) that a reader couldn't verify or that seem fabricated.
- brand_safety_issues: tone problems, unsupported medical/financial/legal \
guarantees, offensive or off-brand content. A first-party mention of, or \
call-to-action for, the PUBLISHING BUSINESS named below is expected and is \
NOT a brand-safety issue.
- duplicate_of: ONLY name a title from the explicit "existing titles" list \
below if the article clearly duplicates it. Do NOT guess, infer, or invent \
duplicates against titles that are not in that list. If no list entry matches, \
this MUST be null.
- confidence: 0.0-1.0 overall readiness to publish unedited. A complete, \
accurate, on-topic, well-structured article should score high (>=0.8); reserve \
low scores for real problems.
- verdict: 'pass' (publish), 'review' (human should look), or 'block' (do not \
publish). Use 'pass' for a solid, complete, accurate article even if it could \
be marginally improved.

The article may include a short pull-quote (a first-party insight from the \
publishing business, or a named real standards body's general guidance) and a \
"Sources & standards referenced" list naming real organizations (e.g. \
National Wood Flooring Association, ANSI). These are expected, not \
unverifiable claims — flag them only if a quote is attributed to a specific \
named individual/study that seems fabricated, or a listed source seems made up.

Be strict about fabricated specifics and truncated/incomplete drafts, but \
don't penalize general, defensible statements or first-party brand mentions."""


def review_article(
    *,
    title: str,
    body_html: str,
    existing_titles: list[str] | None = None,
    cost: CostTracker | None = None,
) -> QAReport:
    settings = get_settings()
    text = html_to_text(body_html)

    # Deterministic banned-topic hard block.
    banned_hits = [
        term
        for term in settings.banned_topics
        if term and term.lower() in (title + " " + text).lower()
    ]

    existing = existing_titles or []
    existing_block = (
        "Existing published titles (the ONLY titles you may cite as a duplicate):\n"
        + "\n".join(f"- {t}" for t in existing[:50])
        if existing
        else "Existing published titles: (none provided — duplicate_of MUST be null)."
    )

    biz = settings.business_name or "the site owner"
    biz_block = f"PUBLISHING BUSINESS: {biz}"
    if settings.business_description:
        biz_block += f" — {settings.business_description}"
    if settings.business_location:
        biz_block += f" (serves {settings.business_location})"
    biz_block += (
        ". Mentions of and calls-to-action for this business are first-party "
        "and expected — do not flag them."
    )

    report: QAReport = structured_invoke(
        model=settings.model_qa,
        schema=QAReport,
        messages=[
            SystemMessage(content=SYSTEM),
            HumanMessage(
                content=f"{biz_block}\n\nTITLE: {title}\n\n{existing_block}\n\n"
                f"ARTICLE TEXT:\n{text[:12000]}"
            ),
        ],
        temperature=0.0,
        stage="qa",
        cost=cost,
    )

    # Enforce banned topics regardless of model output.
    if banned_hits:
        report.brand_safety_issues = list(
            {*report.brand_safety_issues, *(f"banned topic: {t}" for t in banned_hits)}
        )
        report.verdict = "block"
        report.confidence = min(report.confidence, 0.0)

    return report
