"""Outline agent (Haiku).

Generates an H2/H3 structure targeting the primary keyword plus secondary
keywords. When competitor headers are supplied (Phase 3 scraper), they are
provided as reference so the outline covers the topics readers/Google expect.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from blog_pipeline.config import get_settings
from blog_pipeline.llm import CostTracker, structured_invoke
from blog_pipeline.schemas import Outline

SYSTEM = """You are an SEO content strategist. Produce a clear, comprehensive \
article outline (H2 sections with H3 subpoints) that fully covers the topic and \
naturally targets the given keywords. Prioritize reader usefulness and topical \
completeness over keyword repetition.

AI answer engines retrieve articles in independent ~150-400 word chunks (one \
chunk per H2 section), so scope each H2 to what fits that band on its own — \
self-contained, answerable without reading other sections. Size the number of \
H2s to the target length accordingly (roughly one H2 per 150-400 words), and \
split any topic that's too broad for one section into two narrower H2s rather \
than one long one."""


def generate_outline(
    topic: str,
    target_keywords: list[str],
    competitor_headers: list[str] | None = None,
    cost: CostTracker | None = None,
) -> Outline:
    settings = get_settings()

    parts = [
        f"Topic: {topic}",
        f"Target keywords: {', '.join(target_keywords) if target_keywords else '(infer from topic)'}",
        f"Target length: ~{settings.word_count_target} words "
        f"(scale section count accordingly).",
    ]
    if competitor_headers:
        joined = "\n".join(f"- {h}" for h in competitor_headers[:40])
        parts.append(
            "Top-ranking competitor headings (cover these angles, don't copy "
            f"verbatim, and find a gap to differentiate):\n{joined}"
        )

    return structured_invoke(
        model=settings.model_outline,
        schema=Outline,
        messages=[SystemMessage(content=SYSTEM), HumanMessage(content="\n\n".join(parts))],
        temperature=0.5,
        stage="outline",
        cost=cost,
    )
