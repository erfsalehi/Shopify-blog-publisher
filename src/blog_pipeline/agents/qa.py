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
from blog_pipeline.llm import CostTracker, make_llm
from blog_pipeline.schemas import QAReport
from blog_pipeline.utils import html_to_text

SYSTEM = """You are a meticulous content QA reviewer and fact-checker. Review the \
article and return a structured assessment.

Assess:
- unverifiable_claims: specific factual assertions (stats, dates, studies, \
quotes, superlatives) that a reader couldn't verify or that seem fabricated.
- brand_safety_issues: tone problems, unsupported medical/financial/legal \
guarantees, offensive or off-brand content.
- duplicate_of: if the article clearly duplicates one of the existing titles \
provided, name it; otherwise null.
- confidence: 0.0-1.0 overall readiness to publish unedited.
- verdict: 'pass' (publish), 'review' (human should look), or 'block' (do not \
publish).

Be strict about fabricated specifics but don't penalize general, defensible \
statements."""


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
        "Existing published titles (check for duplication):\n"
        + "\n".join(f"- {t}" for t in existing[:50])
        if existing
        else "No existing titles provided."
    )

    llm = make_llm(settings.model_qa, temperature=0.0)
    structured = llm.with_structured_output(QAReport, include_raw=True)
    res = structured.invoke(
        [
            SystemMessage(content=SYSTEM),
            HumanMessage(
                content=f"TITLE: {title}\n\n{existing_block}\n\n"
                f"ARTICLE TEXT:\n{text[:12000]}"
            ),
        ]
    )
    if cost is not None:
        cost.record("qa", settings.model_qa, res["raw"])
    report: QAReport = res["parsed"]

    # Enforce banned topics regardless of model output.
    if banned_hits:
        report.brand_safety_issues = list(
            {*report.brand_safety_issues, *(f"banned topic: {t}" for t in banned_hits)}
        )
        report.verdict = "block"
        report.confidence = min(report.confidence, 0.0)

    return report
