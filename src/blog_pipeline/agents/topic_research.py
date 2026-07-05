"""Topic Research agent (Haiku), invoked by the Calendar agent.

Inputs: niche/vertical, seed keywords, optional competitor URLs.
Process: pull real SERP + keyword-volume data from DataForSEO when available,
scrape competitor headings for content-gap signal, then have the LLM synthesize
and rank topic candidates as structured output. When DataForSEO is unset the
agent still produces candidates from the LLM's own knowledge (volume/difficulty
left null), so a weekly refresh never hard-fails on a missing SERP key.

This is a synthesis agent rather than a free-running ReAct loop: the tools are
called deterministically up front and their results are handed to the model,
which is cheaper and more predictable for a batch/overnight job (PRD 12.1).
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from blog_pipeline.config import get_settings
from blog_pipeline.llm import CostTracker, make_llm
from blog_pipeline.schemas import TopicCandidate, TopicCandidates
from blog_pipeline.tools.dataforseo import DataForSEOClient
from blog_pipeline.tools.scraper import gather_competitor_headers

SYSTEM = """You are an SEO topic strategist. Given a niche, seed keywords, live \
SERP/keyword data, and competitor content structures, propose ranked blog topic \
candidates that (a) have search demand, (b) fit the niche, and (c) exploit a \
content gap competitors miss. Prefer specific, long-tail, intent-driven topics \
over broad head terms. Use the provided search volumes/difficulty where given; \
leave them null when unknown rather than guessing precise numbers."""


def research_topics(
    *,
    niche: str,
    seed_keywords: list[str],
    competitor_urls: list[str] | None = None,
    count: int = 10,
    cost: CostTracker | None = None,
) -> list[TopicCandidate]:
    settings = get_settings()
    dfs = DataForSEOClient()

    context_parts = [f"Niche/vertical: {niche}",
                     f"Seed keywords: {', '.join(seed_keywords)}"]

    if dfs.enabled and seed_keywords:
        kw_data = dfs.keyword_data(seed_keywords)
        if kw_data:
            lines = [f"- {k['keyword']}: volume={k.get('search_volume')}, "
                     f"competition={k.get('competition')}" for k in kw_data]
            context_parts.append("Keyword data (DataForSEO):\n" + "\n".join(lines))
        # SERP for the first seed keyword as a gap signal.
        serp = dfs.serp_top(seed_keywords[0])
        if serp:
            lines = [f"- {r['title']} ({r['url']})" for r in serp[:10]]
            context_parts.append(
                f"Top SERP results for '{seed_keywords[0]}':\n" + "\n".join(lines)
            )

    if competitor_urls:
        headers = gather_competitor_headers(competitor_urls)
        if headers:
            context_parts.append(
                "Competitor headings (find gaps):\n"
                + "\n".join(f"- {h}" for h in headers[:40])
            )

    context_parts.append(
        f"Propose {count} ranked topic candidates as structured output."
    )

    llm = make_llm(settings.model_research, temperature=0.6)
    structured = llm.with_structured_output(TopicCandidates, include_raw=True)
    res = structured.invoke(
        [SystemMessage(content=SYSTEM),
         HumanMessage(content="\n\n".join(context_parts))]
    )
    if cost is not None:
        cost.record("research", settings.model_research, res["raw"])
    return res["parsed"].candidates
