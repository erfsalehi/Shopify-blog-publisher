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
from blog_pipeline.llm import CostTracker, structured_invoke
from blog_pipeline.schemas import TopicCandidate, TopicCandidates
from blog_pipeline.tools.dataforseo import DataForSEOClient
from blog_pipeline.tools.scraper import gather_competitor_headers

SYSTEM = """You are an SEO topic strategist. Given a niche, seed keywords, live \
SERP/keyword data, and competitor content structures, propose ranked blog topic \
candidates that (a) have search demand, (b) fit the niche, and (c) exploit a \
content gap competitors miss. Prefer specific, long-tail, intent-driven topics \
over broad head terms. Use the provided search volumes/difficulty where given; \
leave them null when unknown rather than guessing precise numbers.

If you are given "striking distance" queries, weight them heavily. Those are \
not market estimates — they are terms this exact site is already being shown \
for and not winning. Google already considers the site relevant to them, so a \
focused article has a far shorter path to page one than one chasing a term the \
site has no history with. Prefer covering a striking-distance query properly \
over a higher-volume term the site has never ranked for."""


def _striking_distance_context() -> str:
    """Queries the site already earns impressions for but doesn't win.

    Degrades to "" on any failure — Search Console is optional, and the weekly
    refresh must not hard-fail because performance data is missing or stale.
    """
    try:
        from blog_pipeline.performance import striking_distance_queries

        rows = striking_distance_queries(limit=25)
    except Exception:
        return ""
    if not rows:
        return ""
    lines = [
        f"- \"{r['query']}\": {r['impressions']} impressions, "
        f"avg position {r['position']}, {r['clicks']} clicks"
        for r in rows
    ]
    return (
        "STRIKING DISTANCE — this site's own Google Search Console data. It is "
        "already shown for these terms but sits off page one, so the "
        "impressions are real demand it is currently failing to convert:\n"
        + "\n".join(lines)
    )


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

    # The site's own Search Console history, when there is any. This outranks
    # every other signal here: DataForSEO describes the market, competitor
    # headings describe rivals, but these are terms Google already shows *this*
    # site for from positions nobody clicks.
    striking = _striking_distance_context()
    if striking:
        context_parts.append(striking)

    if competitor_urls:
        headers = gather_competitor_headers(competitor_urls)
        if headers:
            context_parts.append(
                "Competitor headings (find gaps):\n"
                + "\n".join(f"- {h}" for h in headers[:40])
            )

    if settings.local_seo:
        loc = settings.business_location
        context_parts.append(
            "This is a LOCAL business — prioritize topics with commercial or "
            "high informational intent that a nearby customer would search "
            "before buying/hiring" + (f" in {loc}" if loc else "")
            + ". A portion should suit local/seasonal angles; avoid purely "
            "national, generic listicles."
        )

    context_parts.append(
        f"Propose {count} ranked topic candidates as structured output."
    )

    result: TopicCandidates = structured_invoke(
        model=settings.model_research,
        schema=TopicCandidates,
        messages=[SystemMessage(content=SYSTEM),
                  HumanMessage(content="\n\n".join(context_parts))],
        temperature=0.6,
        stage="research",
        cost=cost,
    )
    return result.candidates
