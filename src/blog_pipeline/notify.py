"""Slack notifications (weekly calendar digest + approval requests).

Uses a simple incoming-webhook POST. When SLACK_WEBHOOK_URL is unset, messages
are logged to stdout instead so the pipeline is fully runnable without Slack.
Interactive approve/reject buttons require a hosted endpoint, so the approval
message instead links to a preview and shows the CLI command to run.
"""

from __future__ import annotations

import httpx
from rich.console import Console

from blog_pipeline.config import get_settings

_console = Console()


def _post(text: str, blocks: list | None = None) -> bool:
    settings = get_settings()
    if not settings.has_slack:
        _console.print(f"[dim][slack disabled][/dim] {text}")
        return False
    payload: dict = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    try:
        resp = httpx.post(settings.slack_webhook_url, json=payload, timeout=15.0)
        resp.raise_for_status()
        return True
    except Exception as e:  # notifications must never break the pipeline
        _console.print(f"[yellow]Slack post failed:[/yellow] {e}")
        return False


def send_calendar_digest(added: list[dict], coverage_weeks: float) -> bool:
    """added: [{scheduled_date, topic, primary_keyword}]."""
    if not added:
        text = f"📅 Calendar refresh: queue already full ({coverage_weeks:.1f} weeks ahead). No topics added."
        return _post(text)
    lines = [f"📅 *Calendar refresh* — added {len(added)} topics "
             f"({coverage_weeks:.1f} weeks of coverage):"]
    for a in added:
        lines.append(f"• {a['scheduled_date']} — *{a['topic']}* "
                     f"(kw: {a.get('primary_keyword', '')})")
    lines.append("\n_Reorder, swap, or veto in the calendar before drafting begins._")
    return _post("\n".join(lines))


def send_approval_request(
    article_id: int, title: str, seo_score: float | None,
    confidence: float | None, preview_url: str | None,
) -> bool:
    settings = get_settings()
    preview = preview_url or (
        f"{settings.preview_base_url.rstrip('/')}/article/{article_id}"
        if settings.preview_base_url else "(no preview URL configured)"
    )
    text = (
        f"📝 *Article pending review* (#{article_id})\n"
        f"*{title}*\n"
        f"SEO score: {seo_score}  |  QA confidence: {confidence}\n"
        f"Preview: {preview}\n"
        f"Approve: `blog-pipeline approve {article_id}`   "
        f"Reject: `blog-pipeline reject {article_id}`"
    )
    return _post(text)


def send_coverage_alert(weeks_remaining: float) -> bool:
    return _post(
        f"⚠️ *Calendar coverage low*: only {weeks_remaining:.1f} weeks of "
        f"scheduled content remain. Run the calendar agent to top up."
    )
