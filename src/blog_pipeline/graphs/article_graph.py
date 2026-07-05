"""Article drafting graph: outline -> draft -> seo -> images -> qa -> gate -> publish.

Design notes:
- State is kept to JSON-friendly primitives/dicts so the SqliteSaver
  checkpointer can serialize a paused run and resume it days later.
- Each node updates the backing Article row so progress is durable even if a
  later stage fails; the graph is the orchestrator, the DB is the record.
- The human gate uses LangGraph `interrupt()`. In "auto" mode a high-confidence
  pass skips the interrupt; otherwise the run pauses until `approve`/`reject`
  resumes it with a Command(resume=...).
- Optional stages (images, qa) degrade to no-ops when their keys are absent so
  the same graph runs in a minimal Phase-1 configuration.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from blog_pipeline.agents.draft import generate_draft
from blog_pipeline.agents.images import generate_images
from blog_pipeline.agents.outline import generate_outline
from blog_pipeline.agents.qa import review_article
from blog_pipeline.agents.seo import optimize_seo
from blog_pipeline.config import get_settings
from blog_pipeline.db import Article, ArticleStatus, get_session
from blog_pipeline.db.models import CalendarEntry, EntryStatus
from blog_pipeline.llm import CostTracker
from blog_pipeline.schemas import Draft, ImageSlot, Outline
from blog_pipeline.tools.shopify import ShopifyClient, ShopifyError
from blog_pipeline.utils import slugify


class ArticleState(TypedDict, total=False):
    article_id: int
    topic: str
    target_keywords: list[str]
    competitor_headers: list[str]

    outline: dict
    title: str
    meta_description: str
    body_html: str
    image_slots: list[dict]

    seo_title: str
    seo_description: str
    seo_score: float
    seo_metrics: dict

    images: list[dict]
    featured_file_id: str | None

    qa_report: dict
    confidence: float

    cost_usd: float
    dry_run: bool
    status: str
    result: dict


# ── persistence helper ───────────────────────────────────────────
def _update_article(article_id: int, **fields: Any) -> None:
    with get_session() as s:
        obj = s.get(Article, article_id)
        if obj is None:
            return
        for k, v in fields.items():
            setattr(obj, k, v)


def _add_cost(state: ArticleState, tracker: CostTracker) -> float:
    total = state.get("cost_usd", 0.0) + tracker.with_fee()
    if state.get("article_id"):
        _update_article(state["article_id"], cost_usd=total)
    return total


# ── nodes ────────────────────────────────────────────────────────
def node_outline(state: ArticleState) -> dict:
    cost = CostTracker()
    outline: Outline = generate_outline(
        state["topic"],
        state.get("target_keywords", []),
        competitor_headers=state.get("competitor_headers") or None,
        cost=cost,
    )
    data = outline.model_dump()
    if state.get("article_id"):
        _update_article(state["article_id"], outline=data)
    return {"outline": data, "cost_usd": _add_cost(state, cost)}


def node_draft(state: ArticleState) -> dict:
    cost = CostTracker()
    outline = Outline.model_validate(state["outline"])
    draft: Draft = generate_draft(outline, cost=cost)
    if state.get("article_id"):
        _update_article(
            state["article_id"], title=draft.title, draft_html=draft.body_html
        )
    return {
        "title": draft.title,
        "meta_description": draft.meta_description,
        "body_html": draft.body_html,
        "image_slots": [s.model_dump() for s in draft.image_slots],
        "cost_usd": _add_cost(state, cost),
    }


def node_seo(state: ArticleState) -> dict:
    settings = get_settings()
    outline = state.get("outline", {})
    primary = outline.get("primary_keyword") or (
        state.get("target_keywords") or [state["topic"]]
    )[0]
    secondary = outline.get("secondary_keywords", [])

    link_targets: list[dict] = []
    if settings.has_shopify:
        try:
            client = ShopifyClient()
            link_targets = client.list_link_targets()
            client.close()
        except Exception:
            link_targets = []

    cost = CostTracker()
    result = optimize_seo(
        body_html=state["body_html"],
        title=state["title"],
        meta_description=state.get("meta_description", ""),
        primary_keyword=primary,
        secondary_keywords=secondary,
        link_targets=link_targets,
        cost=cost,
    )
    if state.get("article_id"):
        _update_article(
            state["article_id"],
            seo_title=result.seo_title,
            seo_description=result.seo_description,
            seo_score=result.score,
            draft_html=result.body_html,
        )
    return {
        "body_html": result.body_html,
        "seo_title": result.seo_title,
        "seo_description": result.seo_description,
        "seo_score": result.score,
        "seo_metrics": result.metrics,
        "cost_usd": _add_cost(state, cost),
    }


def node_images(state: ArticleState) -> dict:
    settings = get_settings()
    if not settings.has_images:
        return {}
    slots = [ImageSlot.model_validate(s) for s in state.get("image_slots", [])]
    slug = slugify(state.get("title", state["topic"]))
    body, records, featured = generate_images(
        body_html=state["body_html"], image_slots=slots, slug=slug
    )
    if state.get("article_id"):
        _update_article(state["article_id"], images=records, draft_html=body)
    return {
        "body_html": body,
        "images": records,
        "featured_file_id": featured,
    }


def node_qa(state: ArticleState) -> dict:
    settings = get_settings()
    # QA requires an LLM key; if research/QA disabled, pass through neutral.
    existing_titles: list[str] = []
    if settings.has_shopify:
        try:
            client = ShopifyClient()
            existing_titles = [a["title"] for a in client.list_published()]
            client.close()
        except Exception:
            existing_titles = []

    cost = CostTracker()
    report = review_article(
        title=state.get("seo_title") or state["title"],
        body_html=state["body_html"],
        existing_titles=existing_titles,
        cost=cost,
    )
    data = report.model_dump()
    if state.get("article_id"):
        _update_article(
            state["article_id"],
            qa_confidence_score=report.confidence,
            qa_report=data,
        )
    return {
        "qa_report": data,
        "confidence": report.confidence,
        "cost_usd": _add_cost(state, cost),
    }


def node_gate(state: ArticleState) -> dict:
    """Pause for human approval. Resumed with {'action': 'approve'|'reject', ...}."""
    if state.get("article_id"):
        _update_article(state["article_id"], status=ArticleStatus.pending_review)

    decision = interrupt(
        {
            "article_id": state.get("article_id"),
            "title": state.get("seo_title") or state.get("title"),
            "seo_score": state.get("seo_score"),
            "confidence": state.get("confidence"),
            "qa_report": state.get("qa_report"),
            "prompt": "Approve, edit, or reject this article.",
        }
    )
    action = (decision or {}).get("action", "reject")
    return {"status": "approved" if action == "approve" else "rejected"}


def node_publish(state: ArticleState) -> dict:
    settings = get_settings()
    dry_run = state.get("dry_run", False)

    if not settings.has_shopify and not dry_run:
        _fail(state, "Shopify not configured; cannot publish.")
        return {"status": "failed", "result": {"error": "shopify_not_configured"}}

    try:
        client = ShopifyClient()
    except ShopifyError as e:
        if dry_run:
            client = None  # dry-run can proceed without a live client
        else:
            _fail(state, str(e))
            return {"status": "failed", "result": {"error": str(e)}}

    handle = slugify(state.get("seo_title") or state["title"])
    try:
        if client is None:
            # dry-run without credentials: synthesize the payload preview
            from blog_pipeline.tools.shopify import PublishResult

            result = PublishResult(
                article_id=None, handle=handle, url=None, dry_run=True,
                payload={
                    "article": {
                        "title": state.get("seo_title") or state["title"],
                        "handle": handle,
                        "bodyLength": len(state["body_html"]),
                        "seo": {
                            "title": state.get("seo_title"),
                            "description": state.get("seo_description"),
                        },
                    }
                },
            )
        else:
            result = client.create_article(
                title=state.get("seo_title") or state["title"],
                body_html=state["body_html"],
                summary=state.get("seo_description"),
                handle=handle,
                seo_title=state.get("seo_title"),
                seo_description=state.get("seo_description"),
                image_file_id=state.get("featured_file_id"),
                dry_run=dry_run,
            )
            client.close()
    except ShopifyError as e:
        _fail(state, str(e))
        return {"status": "failed", "result": {"error": str(e)}}

    if dry_run:
        if state.get("article_id"):
            _update_article(state["article_id"], handle=handle)
        return {
            "status": "dry_run",
            "result": {"dry_run": True, "payload": result.payload},
        }

    published_at = datetime.now(timezone.utc)
    if state.get("article_id"):
        _update_article(
            state["article_id"],
            status=ArticleStatus.published,
            shopify_article_id=result.article_id,
            handle=result.handle,
            published_at=published_at,
        )
        _mark_entry_published(state["article_id"])
    return {
        "status": "published",
        "result": {
            "shopify_article_id": result.article_id,
            "url": result.url,
            "handle": result.handle,
        },
    }


def node_rejected(state: ArticleState) -> dict:
    if state.get("article_id"):
        _update_article(state["article_id"], status=ArticleStatus.rejected)
    return {"status": "rejected", "result": {"rejected": True}}


def node_blocked(state: ArticleState) -> dict:
    reason = "QA blocked publication."
    report = state.get("qa_report") or {}
    issues = report.get("brand_safety_issues") or []
    if issues:
        reason += " " + "; ".join(issues)
    _fail(state, reason)
    return {"status": "blocked", "result": {"blocked": True, "reason": reason}}


# ── helpers ──────────────────────────────────────────────────────
def _fail(state: ArticleState, reason: str) -> None:
    if state.get("article_id"):
        _update_article(
            state["article_id"], status=ArticleStatus.failed, failure_reason=reason
        )


def _mark_entry_published(article_id: int) -> None:
    with get_session() as s:
        entry = (
            s.query(CalendarEntry).filter(CalendarEntry.article_id == article_id).first()
        )
        if entry:
            entry.status = EntryStatus.published


# ── routing ──────────────────────────────────────────────────────
def route_after_qa(state: ArticleState) -> str:
    settings = get_settings()
    report = state.get("qa_report") or {}
    verdict = report.get("verdict", "review")
    confidence = state.get("confidence", 0.0)

    if verdict == "block":
        return "blocked"
    # Auto mode: publish straight through when QA is confident enough.
    if (
        settings.gate_mode == "auto"
        and verdict == "pass"
        and confidence >= settings.confidence_threshold
    ):
        return "publish"
    return "gate"


def route_after_gate(state: ArticleState) -> str:
    return "publish" if state.get("status") == "approved" else "rejected"


# ── graph assembly ───────────────────────────────────────────────
def build_article_graph(checkpointer=None):
    g = StateGraph(ArticleState)
    g.add_node("outline", node_outline)
    g.add_node("draft", node_draft)
    g.add_node("seo", node_seo)
    g.add_node("images", node_images)
    g.add_node("qa", node_qa)
    g.add_node("gate", node_gate)
    g.add_node("publish", node_publish)
    g.add_node("rejected", node_rejected)
    g.add_node("blocked", node_blocked)

    g.add_edge(START, "outline")
    g.add_edge("outline", "draft")
    g.add_edge("draft", "seo")
    g.add_edge("seo", "images")
    g.add_edge("images", "qa")
    g.add_conditional_edges(
        "qa", route_after_qa,
        {"publish": "publish", "gate": "gate", "blocked": "blocked"},
    )
    g.add_conditional_edges(
        "gate", route_after_gate, {"publish": "publish", "rejected": "rejected"}
    )
    g.add_edge("publish", END)
    g.add_edge("rejected", END)
    g.add_edge("blocked", END)

    return g.compile(checkpointer=checkpointer)
