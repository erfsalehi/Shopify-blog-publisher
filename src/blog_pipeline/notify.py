"""Slack notifications (weekly calendar digest + low-coverage alerts).

Uses a simple incoming-webhook POST. When SLACK_WEBHOOK_URL is unset, messages
are logged to stdout instead so the pipeline is fully runnable without Slack.
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


def send_coverage_alert(weeks_remaining: float) -> bool:
    return _post(
        f"⚠️ *Calendar coverage low*: only {weeks_remaining:.1f} weeks of "
        f"scheduled content remain. Run the calendar agent to top up."
    )


def send_article_update(topic: str, summary: dict) -> bool:
    """Post a per-article result to Slack after a run-article/run-daily run.

    `summary` is the dict returned by run_article: {status, result, ...}.
    """
    status = summary.get("status")
    r = summary.get("result") or {}
    linear = r.get("linear_identifier")
    linear_url = r.get("url")
    linear_tag = f" (<{linear_url}|{linear}>)" if linear and linear_url else (
        f" ({linear})" if linear else ""
    )

    if status == "published":
        text = (f"✅ *Published live:* {topic}\n{r.get('shopify_url')}"
                f"\nLinear{linear_tag}")
    elif status == "synced":
        drafted = " · Shopify draft created" if r.get("shopify_article_id") else ""
        text = (f"📝 *Drafted:* {topic}\nLinear{linear_tag} → "
                f"*{r.get('linear_state', 'synced')}*{drafted}")
    elif status == "failed":
        text = f"⚠️ *Draft failed:* {topic}\n{r.get('error') or r}"
    else:
        return False
    return _post(text)
