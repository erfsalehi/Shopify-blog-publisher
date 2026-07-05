"""Draft agent (Sonnet — the one reader-visible quality step).

Takes the outline + brand voice guide and produces the full article: title,
meta description, semantic HTML body, and image slot requests with alt text.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from blog_pipeline.config import get_settings
from blog_pipeline.llm import CostTracker, make_llm
from blog_pipeline.prompts import brand_voice
from blog_pipeline.schemas import Draft, Outline

SYSTEM = """You are an expert blog writer. Write a complete, original, factually \
careful article from the given outline. Follow the brand voice guide exactly.

Rules:
- Output the body as clean semantic HTML: <h2>, <h3>, <p>, <ul>/<li>, <ol>.
  Do NOT include <html>, <head>, <body>, or an <h1> (the title is separate).
- Do NOT invent statistics, studies, quotes, or specific claims you can't stand
  behind. Prefer general, defensible statements.
- Propose 2-3 image slots: one 'featured' plus inline images at natural breaks.
  Each needs a vivid text-to-image prompt and concise SEO alt text.
- Write a 150-160 character meta description."""


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


def generate_draft(outline: Outline, cost: CostTracker | None = None) -> Draft:
    settings = get_settings()
    llm = make_llm(settings.model_draft, temperature=0.7)

    voice = brand_voice()
    human = [
        "BRAND VOICE GUIDE:\n" + (voice or "(no guide provided — use a clear, "
        "helpful, professional tone)"),
        "OUTLINE:\n" + _outline_to_text(outline),
        f"Target length: ~{settings.word_count_target} words.",
    ]

    structured = llm.with_structured_output(Draft, include_raw=True)
    result = structured.invoke(
        [SystemMessage(content=SYSTEM), HumanMessage(content="\n\n".join(human))]
    )
    if cost is not None:
        cost.record("draft", settings.model_draft, result["raw"])
    return result["parsed"]
