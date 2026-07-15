"""Pydantic schemas for structured LLM outputs and shared pipeline types.

These are the contracts between stages. Using `.with_structured_output(Schema)`
on the LLM guarantees each agent returns parseable, typed data instead of free
text we'd have to scrape.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class OutlineSection(BaseModel):
    heading: str = Field(description="H2 heading text")
    subpoints: list[str] = Field(
        default_factory=list, description="H3 subheadings or key points to cover"
    )


class Outline(BaseModel):
    working_title: str = Field(description="Provisional article title")
    primary_keyword: str
    secondary_keywords: list[str] = Field(default_factory=list)
    sections: list[OutlineSection] = Field(
        description="Ordered H2 sections, each with subpoints"
    )


class ImageSlot(BaseModel):
    role: str = Field(description="'featured' or 'inline'")
    placement_hint: str = Field(
        description="Where in the article this image belongs (e.g. section heading)"
    )
    prompt: str = Field(description="Text-to-image prompt describing the visual")
    alt: str = Field(description="SEO alt text for the image")


class FAQItem(BaseModel):
    question: str = Field(description="A natural question a reader/AI might ask")
    answer: str = Field(description="A direct, self-contained answer (1-3 sentences)")


class Draft(BaseModel):
    title: str
    meta_description: str = Field(description="150-160 char SEO meta description")
    body_html: str = Field(description="Article body as clean semantic HTML")
    key_takeaways: list[str] = Field(
        default_factory=list,
        description="3-5 concise, self-contained takeaway sentences (answer-first, "
        "extractable by AI answer engines)",
    )
    faq: list[FAQItem] = Field(
        default_factory=list,
        description="3-6 FAQ pairs covering common questions on the topic, for a "
        "visible FAQ section + FAQPage structured data",
    )
    pull_quote: str = Field(
        default="",
        description="One short (1-2 sentence), quotable, authoritative insight — "
        "either attributed to the publishing business's own expertise (first-party, "
        "e.g. 'Our installers find that...') or naming a real, well-known industry "
        "standards body. Never a fabricated named individual/study. Empty string if "
        "nothing genuinely quotable fits.",
    )
    sources: list[str] = Field(
        default_factory=list,
        description="Real, well-known authoritative organizations/standards named "
        "in the article body (e.g. 'National Wood Flooring Association (NWFA)', "
        "'ANSI'). Only include ones actually referenced in body_html. Empty if none.",
    )
    image_slots: list[ImageSlot] = Field(
        default_factory=list, description="Featured + inline image requests"
    )


class TopicCandidate(BaseModel):
    topic: str
    primary_keyword: str
    secondary_keywords: list[str] = Field(default_factory=list)
    search_volume: int | None = None
    difficulty: float | None = Field(
        default=None, description="Keyword difficulty 0-100"
    )
    rationale: str = Field(description="Why this topic — content gap / opportunity")


class TopicCandidates(BaseModel):
    candidates: list[TopicCandidate]


class SeedKeywords(BaseModel):
    keywords: list[str] = Field(
        description="Diverse seed keywords real customers search for in this niche"
    )


class RevisedDraft(BaseModel):
    body_html: str = Field(
        description="The revised article body as clean semantic HTML "
        "(<h2>/<h3>/<p>/<ul>/<ol>), no <html>/<head>/<body>/<h1>"
    )
    pull_quote: str = Field(
        default="",
        description="Same rules as the draft agent's pull_quote. Only set this "
        "if you're adding/improving one; leave empty to keep the existing quote.",
    )
    sources: list[str] = Field(
        default_factory=list,
        description="Same rules as the draft agent's sources. Only set this if "
        "you're adding real named sources; leave empty to keep the existing list.",
    )


class RefreshedArticle(BaseModel):
    body_html: str = Field(
        description="The refreshed article body as clean semantic HTML "
        "(<h2>/<h3>/<p>/<ul>/<ol>), no <html>/<head>/<body>/<h1>. Return the "
        "COMPLETE article, not a fragment or a diff."
    )
    change_summary: list[str] = Field(
        default_factory=list,
        description="3-8 short bullets naming what actually changed and why, "
        "specific enough for a reviewer to spot-check (e.g. 'Split the "
        "installation section into prep and laying'). Not vague claims like "
        "'improved SEO'.",
    )
    seo_title: str = Field(
        default="",
        description="An improved title, ONLY if the current one is genuinely "
        "weak. Empty means keep the existing title — renaming a page that "
        "already ranks is a real cost, so leave it alone unless it's clearly "
        "better.",
    )
    meta_description: str = Field(
        default="",
        description="An improved 150-160 char meta description, or empty to "
        "keep the existing one.",
    )
    skipped: bool = Field(
        default=False,
        description="True if this article genuinely needs no refresh. Prefer "
        "this over inventing busywork edits — an honest no-op is a valid, "
        "useful answer.",
    )


class QAReport(BaseModel):
    confidence: float = Field(description="Overall publish confidence 0.0-1.0")
    unverifiable_claims: list[str] = Field(default_factory=list)
    brand_safety_issues: list[str] = Field(default_factory=list)
    duplicate_of: str | None = Field(
        default=None, description="Title/topic this closely duplicates, if any"
    )
    verdict: str = Field(description="'pass', 'review', or 'block'")
    notes: str = ""
