"""Article drafting graph: outline -> draft -> seo -> images -> qa -> [publish?] -> sync.

Design notes:
- State is kept to JSON-friendly primitives/dicts so the SqliteSaver
  checkpointer can serialize a run and survive a mid-run crash.
- Each node updates the backing Article row so progress is durable even if a
  later stage fails; the graph is the orchestrator, the DB is the record.
- There is no human-approval interrupt. After QA the graph routes on the QA
  outcome: a confident pass (verdict 'pass', confidence >= threshold) that
  has Shopify configured publishes live to Shopify, then records that on the
  Linear issue (moved to the published state, with the live URL). Anything
  else — low confidence, verdict 'review', verdict 'block', or Shopify not
  configured — skips publishing and just syncs to Linear for a human to
  review and publish by hand.
- Optional stages (images, qa) degrade to no-ops when their keys are absent so
  the same graph runs in a minimal Phase-1 configuration.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from blog_pipeline.agents.draft import generate_draft
from blog_pipeline.agents.images import generate_images
from blog_pipeline.agents.outline import generate_outline
from blog_pipeline.agents.qa import review_article
from blog_pipeline.agents.revise import revise_article
from blog_pipeline.agents.seo import optimize_seo
from blog_pipeline.config import get_settings
from blog_pipeline.db import Article, ArticleStatus, get_session
from blog_pipeline.db.models import CalendarEntry, EntryStatus
from blog_pipeline.llm import CostTracker
from blog_pipeline.agents.geo import apply_geo
from blog_pipeline.schemas import Draft, FAQItem, ImageSlot, Outline
from blog_pipeline.tools.linear import IssueResult, LinearClient, LinearError
from blog_pipeline.tools.shopify import ShopifyClient, ShopifyError
from blog_pipeline.utils import html_to_markdown, slugify

BLOG_LABEL = "Blog"


class ArticleState(TypedDict, total=False):
    article_id: int
    topic: str
    target_keywords: list[str]
    competitor_headers: list[str]
    linear_issue_id: str | None

    outline: dict
    title: str
    meta_description: str
    body_html: str
    image_slots: list[dict]
    key_takeaways: list[str]
    faq: list[dict]
    pull_quote: str
    sources: list[str]

    seo_title: str
    seo_description: str
    seo_score: float
    seo_metrics: dict
    revision_count: int

    images: list[dict]
    featured_image_url: str | None

    qa_report: dict
    confidence: float

    published: bool
    shopify_draft: bool
    shopify_article_id: str | None
    shopify_url: str | None
    publish_error: str | None

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
def _gather_competitor_headers(topic: str, keywords: list[str]) -> list[str]:
    """Real SERP grounding: top-ranking competitor H2/H3s for the primary
    keyword, so the outline/draft cover the angles Google already rewards
    instead of generic filler. Falls back to SERP result titles when pages
    can't be scraped; returns [] (no grounding) if DataForSEO is unconfigured
    or anything errors — never blocks the run."""
    settings = get_settings()
    if not settings.has_dataforseo:
        return []
    primary = (keywords or [topic])[0]
    try:
        from blog_pipeline.tools.dataforseo import DataForSEOClient
        from blog_pipeline.tools.scraper import gather_competitor_headers

        serp = DataForSEOClient().serp_top(primary, depth=5)
        urls = [r["url"] for r in serp[:3] if r.get("url")]
        headers = gather_competitor_headers(urls) if urls else []
        if not headers:
            headers = [r["title"] for r in serp[:10] if r.get("title")]
        return headers[:40]
    except Exception:
        return []


def node_outline(state: ArticleState) -> dict:
    cost = CostTracker()
    competitor_headers = state.get("competitor_headers") or _gather_competitor_headers(
        state["topic"], state.get("target_keywords", [])
    )
    outline: Outline = generate_outline(
        state["topic"],
        state.get("target_keywords", []),
        competitor_headers=competitor_headers or None,
        cost=cost,
    )
    data = outline.model_dump()
    if state.get("article_id"):
        _update_article(state["article_id"], outline=data)
    return {
        "outline": data,
        "competitor_headers": competitor_headers,
        "cost_usd": _add_cost(state, cost),
    }


def node_draft(state: ArticleState) -> dict:
    cost = CostTracker()
    outline = Outline.model_validate(state["outline"])
    draft: Draft = generate_draft(
        outline, competitor_headers=state.get("competitor_headers") or None, cost=cost
    )
    if state.get("article_id"):
        _update_article(
            state["article_id"], title=draft.title, draft_html=draft.body_html
        )
    return {
        "title": draft.title,
        "meta_description": draft.meta_description,
        "body_html": draft.body_html,
        "image_slots": [s.model_dump() for s in draft.image_slots],
        "key_takeaways": draft.key_takeaways,
        "faq": [f.model_dump() for f in draft.faq],
        "pull_quote": draft.pull_quote,
        "sources": draft.sources,
        "cost_usd": _add_cost(state, cost),
    }


def node_seo(state: ArticleState) -> dict:
    settings = get_settings()
    outline = state.get("outline", {})
    primary = outline.get("primary_keyword") or (
        state.get("target_keywords") or [state["topic"]]
    )[0]
    secondary = outline.get("secondary_keywords", [])

    # Internal-link anchors from the store's own catalog (products + pages).
    # Best-effort: a Shopify hiccup just means no internal links this run.
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
        pull_quote=state.get("pull_quote", ""),
        sources=state.get("sources", []),
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


def node_revise(state: ArticleState) -> dict:
    """Rewrite the body to fix the rubric's flagged weaknesses, then loop back
    to SEO to re-score. Capped at one pass by route_after_seo."""
    outline = state.get("outline", {})
    primary = outline.get("primary_keyword") or (
        state.get("target_keywords") or [state["topic"]]
    )[0]
    secondary = outline.get("secondary_keywords", [])

    cost = CostTracker()
    new_html, new_quote, new_sources = revise_article(
        body_html=state["body_html"],
        primary_keyword=primary,
        secondary_keywords=secondary,
        metrics=state.get("seo_metrics", {}),
        pull_quote=state.get("pull_quote", ""),
        sources=state.get("sources", []),
        cost=cost,
    )
    if state.get("article_id"):
        _update_article(state["article_id"], draft_html=new_html)
    return {
        "body_html": new_html,
        "pull_quote": new_quote,
        "sources": new_sources,
        "revision_count": state.get("revision_count", 0) + 1,
        "cost_usd": _add_cost(state, cost),
    }


def route_after_seo(state: ArticleState) -> str:
    """One revision pass if the article scored below the SEO pass mark."""
    settings = get_settings()
    if (
        state.get("seo_score", 100.0) < settings.seo_min_score
        and state.get("revision_count", 0) < 1
    ):
        return "revise"
    return "images"


def node_images(state: ArticleState) -> dict:
    settings = get_settings()
    slots = [ImageSlot.model_validate(s) for s in state.get("image_slots", [])]
    if not slots:
        return {}
    slug = slugify(state.get("title", state["topic"]))

    if settings.has_images:
        # Generate real images and host them (Gemini via OpenRouter + Linear).
        body, records, featured = generate_images(
            body_html=state["body_html"], image_slots=slots, slug=slug
        )
    elif settings.image_placeholders:
        # Don't generate — drop bold [bracketed] prompts into the body for the
        # user to generate (e.g. with Shopify AI) and swap in before publishing.
        from blog_pipeline.agents.images import place_image_prompts

        body, records, featured = place_image_prompts(
            body_html=state["body_html"], image_slots=slots
        )
    else:
        return {}

    if state.get("article_id"):
        _update_article(state["article_id"], images=records, draft_html=body)
    return {
        "body_html": body,
        "images": records,
        "featured_image_url": featured,
    }


def node_geo(state: ArticleState) -> dict:
    """Enrichment before QA: append a "Shop with us" CTA (store promotion), then
    add AI-SEO artifacts — a Key-takeaways box, a pull-quote, a sources list,
    an FAQ section, and JSON-LD (Article + FAQPage) structured data so AI
    answer engines can parse and cite the page. Runs after images and before
    QA (so QA reviews it all)."""
    from blog_pipeline.agents.promote import render_shop_cta

    settings = get_settings()
    body = state["body_html"]

    # Store promotion: closing CTA sits at the end of the main content, before
    # the FAQ that GEO appends.
    cta = render_shop_cta()
    if cta:
        body += cta

    if settings.enable_geo:
        faq = [FAQItem.model_validate(f) for f in state.get("faq", [])]
        body = apply_geo(
            body_html=body,
            title=state.get("seo_title") or state.get("title") or state["topic"],
            description=state.get("seo_description") or state.get("meta_description", ""),
            takeaways=state.get("key_takeaways", []),
            faq=faq,
            pull_quote=state.get("pull_quote", ""),
            sources=state.get("sources", []),
        )

    if body == state["body_html"]:
        return {}
    if state.get("article_id"):
        _update_article(state["article_id"], draft_html=body)
    return {"body_html": body}


def node_qa(state: ArticleState) -> dict:
    existing_titles: list[str] = []
    with get_session() as s:
        query = s.query(Article.title).filter(
            Article.status == ArticleStatus.synced, Article.title.isnot(None)
        )
        if state.get("article_id"):
            query = query.filter(Article.id != state["article_id"])
        existing_titles = [row[0] for row in query.all()]

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


def _target_state(verdict: str, confidence: float, threshold: float) -> str:
    """Linear-only path (no Shopify publish): map QA outcome to a configured
    workflow state that actually exists on the team."""
    settings = get_settings()
    if verdict == "block":
        return settings.linear_blocked_state
    if verdict == "pass" and confidence >= threshold:
        return settings.linear_review_state
    return settings.linear_needs_work_state


def _yes_no(value: object) -> str:
    return "yes" if value else "no"


# (metrics key, label, formatter, the band score_seo grades it against).
# Mirrors the rubric in agents/seo.py — if the bands there move, move these.
_SEO_METRIC_ROWS: list[tuple[str, str, Callable[[Any], str], str]] = [
    ("word_count", "Word count", lambda v: f"{v:,}", "60-140% of target"),
    ("kw_in_title", "Primary keyword in title", _yes_no, "yes"),
    ("kw_in_intro", "Primary keyword in first 100 words", _yes_no, "yes"),
    ("keyword_density", "Keyword density", lambda v: f"{float(v) * 100:.2f}%", "0.30-2.50%"),
    ("secondary_coverage", "Secondary keywords covered", lambda v: f"{float(v) * 100:.0f}%", "100%"),
    ("h2_count", "H2 sections", str, "2 or more"),
    ("flesch_reading_ease", "Reading ease (Flesch)", str, "50 or higher"),
    ("meta_description_length", "Meta description", lambda v: f"{v} chars", "120-160"),
    ("internal_links", "Internal links", str, "2 or more"),
    ("has_pull_quote", "Pull quote (GEO)", _yes_no, "yes"),
    ("source_count", "Named sources (GEO)", str, "1 or more"),
    ("chunk_compliant_sections", "Sections in the 150-400 word band (GEO)", str, "all of them"),
]


def _seo_metrics_table(metrics: dict) -> list[str]:
    """Render score_seo's metrics dict as a table.

    Every row is already computed and already decides whether the article
    clears seo_min_score, but none of it reached a human — so a gated article
    landed in Linear with a bare number and no indication of which lever
    missed. A formatter that raises on an unexpected value would take the
    whole issue down with it, so each row degrades to the raw repr instead.
    """
    rows = []
    for key, label, fmt, target in _SEO_METRIC_ROWS:
        if (value := metrics.get(key)) is None:
            continue
        try:
            rendered = fmt(value)
        except (TypeError, ValueError):
            rendered = repr(value)
        rows.append(f"| {label} | {rendered} | {target} |")
    if not rows:
        return []
    return ["", "| Metric | Value | Target |", "| --- | --- | --- |", *rows]


def _build_description(state: ArticleState) -> str:
    outline = state.get("outline") or {}
    primary = outline.get("primary_keyword") or (
        state.get("target_keywords") or [state.get("topic", "")]
    )[0]
    secondary = outline.get("secondary_keywords") or []
    qa = state.get("qa_report") or {}

    lines = [f"**Primary keyword:** {primary}"]
    if secondary:
        lines.append(f"**Secondary keywords:** {', '.join(secondary)}")
    if state.get("seo_score") is not None:
        # The gate is what makes the number mean anything to a reader deciding
        # whether to rewrite or ship.
        lines.append(
            f"**SEO score:** {state['seo_score']}/100 "
            f"(passes at {get_settings().seo_min_score})"
        )
    if state.get("confidence") is not None:
        lines.append(f"**QA confidence:** {state['confidence']}")
    lines.append("\n---")

    featured = state.get("featured_image_url")
    # Only embed a real hosted image; a placeholder "featured" is just prompt
    # text and already appears inline in the body.
    if featured and str(featured).startswith("http"):
        lines.append(f"\n![featured image]({featured})")

    if state.get("published"):
        lines.insert(0, f"✅ **Published live to Shopify:** {state.get('shopify_url')}\n")
    elif state.get("shopify_draft"):
        lines.insert(
            0,
            "📝 **Created in Shopify as an UNPUBLISHED draft.** Open Shopify admin "
            "→ Content → Blog posts, review it, and click **Publish** when ready "
            f"(it'll live at {state.get('shopify_url')}).\n",
        )
    elif state.get("publish_error"):
        lines.insert(
            0,
            f"⚠️ **Auto-publish to Shopify failed:** {state['publish_error']} — "
            "review and publish this one manually.\n",
        )

    lines.append("\n" + html_to_markdown(state.get("body_html", "")))

    lines.append("\n---\n### SEO meta")
    lines.append(f"- **SEO title:** {state.get('seo_title') or state.get('title', '')}")
    lines.append(f"- **Meta description:** {state.get('seo_description', '')}")
    lines.extend(_seo_metrics_table(state.get("seo_metrics") or {}))

    images = state.get("images") or []
    hosted = [i for i in images if i.get("url")]
    placeholders = [i for i in images if not i.get("url")]
    if hosted:
        lines.append("\n### Images")
        for img in hosted:
            lines.append(f"- {img.get('role')}: [{img.get('alt')}]({img.get('url')})")
    if placeholders:
        lines.append(
            "\n### Image prompts (generate & insert before publishing)\n"
            "The bold `[IMAGE — …]` markers in the body are prompts. Generate each "
            "with Shopify's AI image tool and replace the marker."
        )
        for img in placeholders:
            lines.append(f"- **{img.get('role')}**: {img.get('prompt')} _(alt: {img.get('alt')})_")

    if qa:
        lines.append("\n### QA notes")
        if qa.get("notes"):
            lines.append(qa["notes"])
        if qa.get("unverifiable_claims"):
            lines.append("**Unverifiable claims flagged:**")
            lines.extend(f"- {c}" for c in qa["unverifiable_claims"])
        if qa.get("brand_safety_issues"):
            lines.append("**Brand safety issues flagged:**")
            lines.extend(f"- {c}" for c in qa["brand_safety_issues"])
        if qa.get("duplicate_of"):
            lines.append(f"**Possible duplicate of:** {qa['duplicate_of']}")

    if state.get("published"):
        lines.append(
            "\n### Status\n"
            "Auto-published live to Shopify (QA passed with high confidence). "
            "Edit in Shopify admin if you want to revise the live post."
        )
    elif state.get("shopify_draft"):
        lines.append(
            "\n### Publish checklist\n"
            "- [ ] Review the unpublished draft in Shopify admin\n"
            "- [ ] Click Publish in Shopify when ready\n"
            "- [ ] Move this issue to Done"
        )
    else:
        lines.append(
            "\n### Publish checklist\n"
            "- [ ] Review and edit the copy above\n"
            "- [ ] Copy the HTML below into your CMS\n"
            "- [ ] Set the featured image\n"
            "- [ ] Publish, then mark this issue Done"
        )
    lines.append(
        "\n<details><summary>Raw HTML</summary>\n\n```html\n"
        + state.get("body_html", "") + "\n```\n</details>"
    )
    return "\n".join(lines)


def node_publish_shopify(state: ArticleState) -> dict:
    """Push the article to Shopify. Reached only for confident passes when
    Shopify is configured (see route_after_qa). Goes live when
    SHOPIFY_PUBLISH_LIVE is true; otherwise creates it unpublished (hidden)
    for a human to review and publish from Shopify admin. A failure is
    non-fatal: it's recorded on state so the Linear sync surfaces it as
    'Needs Adjustments' for manual publishing rather than losing work."""
    settings = get_settings()
    live = settings.shopify_publish_live
    dry_run = state.get("dry_run", False)
    title = state.get("seo_title") or state.get("title") or state["topic"]
    handle = slugify(title)

    try:
        client = ShopifyClient()
    except ShopifyError as e:
        return {"published": False, "publish_error": str(e)}

    try:
        result = client.create_article(
            title=title,
            body_html=state["body_html"],
            summary=state.get("seo_description"),
            handle=handle,
            seo_title=state.get("seo_title"),
            seo_description=state.get("seo_description"),
            published=live,
            dry_run=dry_run,
        )
    except ShopifyError as e:
        return {"published": False, "publish_error": str(e)}
    finally:
        client.close()

    if dry_run:
        return {
            "published": False,
            "result": {"shopify_payload": result.payload, "publish_live": live},
        }

    if live:
        if state.get("article_id"):
            _update_article(
                state["article_id"],
                status=ArticleStatus.published,
                shopify_article_id=result.article_id,
                shopify_url=result.url,
                handle=result.handle,
                published_at=datetime.now(timezone.utc),
            )
        return {
            "published": True,
            "shopify_article_id": result.article_id,
            "shopify_url": result.url,
        }

    # Hidden-draft mode: real article created in Shopify, not public. It still
    # needs a human to click Publish, so it's a Linear to-do, not Done.
    if state.get("article_id"):
        _update_article(
            state["article_id"],
            shopify_article_id=result.article_id,
            shopify_url=result.url,
            handle=result.handle,
        )
    return {
        "published": False,
        "shopify_draft": True,
        "shopify_article_id": result.article_id,
        "shopify_url": result.url,
    }


def route_after_qa(state: ArticleState) -> str:
    """Confident pass + Shopify configured -> auto-publish; else Linear only."""
    settings = get_settings()
    report = state.get("qa_report") or {}
    verdict = report.get("verdict", "review")
    confidence = state.get("confidence", 0.0)
    if (
        settings.can_autopublish
        and verdict == "pass"
        and confidence >= settings.confidence_threshold
    ):
        return "publish"
    return "sync_linear"


def node_sync_linear(state: ArticleState) -> dict:
    settings = get_settings()
    dry_run = state.get("dry_run", False)
    report = state.get("qa_report") or {}
    verdict = report.get("verdict", "review")
    confidence = state.get("confidence", 0.0)
    if state.get("published"):
        target_state = settings.linear_published_state
    elif state.get("shopify_draft"):
        # Real article created in Shopify but unpublished — human needs to
        # click Publish, so it's a review item, not Done.
        target_state = settings.linear_review_state
    elif state.get("publish_error"):
        # Was confident enough to publish but the publish call failed —
        # flag it for a human to publish by hand rather than silently.
        target_state = settings.linear_needs_work_state
    else:
        target_state = _target_state(verdict, confidence, settings.confidence_threshold)
    title = state.get("seo_title") or state.get("title") or state["topic"]
    description = _build_description(state)

    if not settings.has_linear and not dry_run:
        _fail(state, "Linear not configured; cannot sync.")
        return {"status": "failed", "result": {"error": "linear_not_configured"}}

    try:
        client = LinearClient()
    except LinearError as e:
        if dry_run:
            client = None  # dry-run can proceed without a live client
        else:
            _fail(state, str(e))
            return {"status": "failed", "result": {"error": str(e)}}

    try:
        if client is None:
            result = IssueResult(
                id=state.get("linear_issue_id"), identifier=None, url=None,
                dry_run=True,
                payload={
                    "issue": {
                        "title": title,
                        "state": target_state,
                        "descriptionLength": len(description),
                    }
                },
            )
        elif state.get("linear_issue_id"):
            result = client.update_issue(
                state["linear_issue_id"], title=title, description=description,
                state=target_state, labels=[BLOG_LABEL], dry_run=dry_run,
            )
        else:
            result = client.create_issue(
                title=title, description=description, state=target_state,
                labels=[BLOG_LABEL], dry_run=dry_run,
            )
        if client is not None and not dry_run and verdict == "block":
            issues = report.get("brand_safety_issues") or []
            note = "QA blocked this draft." + (" " + "; ".join(issues) if issues else "")
            try:
                client.add_comment(result.id, note)
            except LinearError:
                pass
    except LinearError as e:
        _fail(state, str(e))
        return {"status": "failed", "result": {"error": str(e)}}
    finally:
        if client is not None:
            client.close()

    if dry_run:
        if state.get("article_id"):
            _update_article(state["article_id"], handle=slugify(title))
        preview: dict = {"dry_run": True, "linear_payload": result.payload,
                         "linear_state": target_state}
        shopify_preview = (state.get("result") or {}).get("shopify_payload")
        if shopify_preview is not None:
            preview["shopify_payload"] = shopify_preview
            preview["would_publish"] = True
        return {"status": "dry_run", "result": preview}

    published = bool(state.get("published"))
    synced_at = datetime.now(timezone.utc)
    if state.get("article_id"):
        _update_article(
            state["article_id"],
            status=ArticleStatus.published if published else ArticleStatus.synced,
            linear_issue_id=result.id,
            linear_identifier=result.identifier,
            linear_url=result.url,
            synced_at=synced_at,
        )
        _mark_entry_synced(state["article_id"], result, published=published)
    return {
        "status": "published" if published else "synced",
        "result": {
            "linear_issue_id": result.id,
            "linear_identifier": result.identifier,
            "url": result.url,
            "linear_state": target_state,
            "shopify_url": state.get("shopify_url"),
            "shopify_article_id": state.get("shopify_article_id"),
            "publish_error": state.get("publish_error"),
        },
    }


# ── helpers ──────────────────────────────────────────────────────
def _fail(state: ArticleState, reason: str) -> None:
    if state.get("article_id"):
        _update_article(
            state["article_id"], status=ArticleStatus.failed, failure_reason=reason
        )


def _mark_entry_synced(
    article_id: int, result: IssueResult, published: bool = False
) -> None:
    with get_session() as s:
        entry = (
            s.query(CalendarEntry).filter(CalendarEntry.article_id == article_id).first()
        )
        if entry:
            entry.status = EntryStatus.published if published else EntryStatus.drafted
            if not entry.linear_issue_id:
                entry.linear_issue_id = result.id
                entry.linear_identifier = result.identifier
                entry.linear_url = result.url


# ── graph assembly ───────────────────────────────────────────────
def build_article_graph(checkpointer=None):
    g = StateGraph(ArticleState)
    g.add_node("outline", node_outline)
    g.add_node("draft", node_draft)
    g.add_node("seo", node_seo)
    g.add_node("images", node_images)
    g.add_node("revise", node_revise)
    g.add_node("geo", node_geo)
    g.add_node("qa", node_qa)
    g.add_node("publish", node_publish_shopify)
    g.add_node("sync_linear", node_sync_linear)

    g.add_edge(START, "outline")
    g.add_edge("outline", "draft")
    g.add_edge("draft", "seo")
    g.add_conditional_edges(
        "seo", route_after_seo, {"revise": "revise", "images": "images"}
    )
    g.add_edge("revise", "seo")
    g.add_edge("images", "geo")
    g.add_edge("geo", "qa")
    g.add_conditional_edges(
        "qa", route_after_qa,
        {"publish": "publish", "sync_linear": "sync_linear"},
    )
    g.add_edge("publish", "sync_linear")
    g.add_edge("sync_linear", END)

    return g.compile(checkpointer=checkpointer)
