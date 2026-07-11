"""Seed Keyword Research agent, invoked by the Calendar agent when no seed
keywords are configured — so `run-calendar --niche "..."` works without
requiring the user to hand-pick starting keywords first. Its output feeds
straight into the Topic Research agent's `seed_keywords` argument.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from blog_pipeline.config import get_settings
from blog_pipeline.llm import CostTracker, structured_invoke
from blog_pipeline.schemas import SeedKeywords

SYSTEM = """You are an SEO keyword strategist. Given a niche/vertical (and \
optionally competitor sites), propose a diverse set of realistic seed \
keywords real customers actually search for — a mix of broad category terms \
and specific long-tail intents (problem-driven, comparison, how-to, \
buying-guide phrasing). These seed keywords are the starting point for \
downstream topic research, so favor terms with clear commercial or \
informational search intent over vague or branded terms."""


def research_seed_keywords(
    *,
    niche: str,
    competitor_urls: list[str] | None = None,
    count: int = 8,
    cost: CostTracker | None = None,
) -> list[str]:
    settings = get_settings()
    human = [f"Niche/vertical: {niche}"]
    if competitor_urls:
        human.append("Competitor sites: " + ", ".join(competitor_urls))
    if settings.local_seo:
        loc = settings.business_location
        human.append(
            "This is a LOCAL business. Favor commercial and informational "
            "intent, and include some location-qualified / 'near me' / "
            "service-intent variants"
            + (f" for {loc}" if loc else "")
            + " alongside broader terms."
        )
    human.append(f"Propose {count} seed keywords.")

    result: SeedKeywords = structured_invoke(
        model=settings.model_research,
        schema=SeedKeywords,
        messages=[SystemMessage(content=SYSTEM), HumanMessage(content="\n\n".join(human))],
        temperature=0.5,
        stage="seed_research",
        cost=cost,
    )
    return [k.strip() for k in result.keywords if k.strip()]
