"""Command-line entry point (Typer).

Commands map to the pipeline's operational surface:
  init-db       create tables
  run-article   draft+publish one topic now (Phase 1 core; supports --dry-run)
  run-calendar  weekly topic-queue refresh (Content Calendar agent)
  run-daily     draft every calendar entry due today
  add-topic     manually queue a topic on a date
  pending       list articles awaiting human approval
  approve/reject resume a paused run at the human gate
  status        health metrics dashboard
  calendar      show the upcoming scheduled queue
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime

import typer

# Windows consoles default to cp1252, which can't encode the Unicode glyphs
# (✓, ⚠, em dash, emoji) used in output/Slack fallbacks. Force UTF-8 so the
# CLI renders correctly regardless of the host terminal's code page.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
from rich.console import Console
from rich.table import Table

from blog_pipeline.config import get_settings
from blog_pipeline.db import get_session, init_db as _init_db
from blog_pipeline.db.models import (
    Article,
    ArticleStatus,
    CalendarEntry,
    EntryStatus,
    TopicSource,
)

app = typer.Typer(add_completion=False, help="Shopify blog Topic→Publish pipeline")
console = Console()


@app.command("init-db")
def init_db_cmd() -> None:
    """Create database tables."""
    _init_db()
    console.print("[green]Database initialized.[/green]")


@app.command("run-article")
def run_article_cmd(
    topic: str = typer.Option(..., "--topic", "-t", help="Article topic"),
    keywords: str = typer.Option("", "--keywords", "-k", help="Comma-separated"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Don't publish; print payload"),
) -> None:
    """Draft and publish a single article from a manual topic."""
    from blog_pipeline.graphs.runner import run_article

    _init_db()
    kw = [k.strip() for k in keywords.split(",") if k.strip()]
    console.print(f"[bold]Running article pipeline[/bold] for: {topic}")
    result = run_article(topic, kw, dry_run=dry_run, source=TopicSource.manual)
    _print_run_result(result)


@app.command("run-calendar")
def run_calendar_cmd(
    niche: str = typer.Option("", "--niche"),
    seeds: str = typer.Option("", "--seeds", help="Comma-separated seed keywords"),
    no_semantic: bool = typer.Option(False, "--no-semantic",
                                     help="Skip embedding-based dedup"),
) -> None:
    """Weekly Content Calendar refresh: fill the topic queue to target."""
    from blog_pipeline.graphs.calendar_graph import run_calendar

    _init_db()
    seed_list = [s.strip() for s in seeds.split(",") if s.strip()]
    result = run_calendar(
        niche=niche or None,
        seed_keywords=seed_list or None,
        use_semantic=not no_semantic,
    )
    console.print_json(json.dumps(result, default=str))


@app.command("run-daily")
def run_daily_cmd(
    on: str = typer.Option("", "--on", help="Date YYYY-MM-DD (default today)"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Draft+publish every calendar entry due today (Daily Publish trigger)."""
    from blog_pipeline.graphs.calendar_graph import get_due_entries
    from blog_pipeline.graphs.runner import run_article

    _init_db()
    target = datetime.strptime(on, "%Y-%m-%d").date() if on else date.today()
    due = get_due_entries(target)
    if not due:
        console.print(f"[dim]No entries due on {target}. No-op.[/dim]")
        return
    console.print(f"[bold]{len(due)} entries due on {target}[/bold]")
    for entry in due:
        console.print(f"→ Drafting: {entry['topic']}")
        result = run_article(
            entry["topic"], entry["target_keywords"],
            source=TopicSource.auto_researched, entry_id=entry["id"], dry_run=dry_run,
        )
        _print_run_result(result)


@app.command("add-topic")
def add_topic_cmd(
    topic: str = typer.Option(..., "--topic", "-t"),
    on: str = typer.Option(..., "--on", help="Scheduled date YYYY-MM-DD"),
    keywords: str = typer.Option("", "--keywords", "-k"),
) -> None:
    """Manually add a topic to the calendar on a given date."""
    from blog_pipeline.graphs.calendar_graph import _get_or_create_calendar

    _init_db()
    sched = datetime.strptime(on, "%Y-%m-%d").date()
    kw = [k.strip() for k in keywords.split(",") if k.strip()]
    with get_session() as s:
        cal = _get_or_create_calendar(s)
        entry = CalendarEntry(
            calendar_id=cal.id, scheduled_date=sched, topic=topic,
            target_keywords=kw, source=TopicSource.manual, status=EntryStatus.queued,
        )
        s.add(entry)
    console.print(f"[green]Queued[/green] '{topic}' for {sched}.")


@app.command("pending")
def pending_cmd() -> None:
    """List articles awaiting human approval."""
    with get_session() as s:
        rows = (
            s.query(Article)
            .filter(Article.status == ArticleStatus.pending_review)
            .all()
        )
        if not rows:
            console.print("[dim]No articles pending review.[/dim]")
            return
        table = Table("ID", "Title", "SEO", "Confidence", "Topic")
        for a in rows:
            table.add_row(
                str(a.id), (a.seo_title or a.title or "")[:50],
                str(a.seo_score), str(a.qa_confidence_score), a.topic[:40],
            )
        console.print(table)


@app.command("approve")
def approve_cmd(article_id: int, note: str = typer.Option("", "--note")) -> None:
    """Approve a paused article and publish it."""
    from blog_pipeline.graphs.runner import resume_article

    result = resume_article(article_id, "approve", note)
    _print_run_result(result)


@app.command("reject")
def reject_cmd(article_id: int, note: str = typer.Option("", "--note")) -> None:
    """Reject a paused article."""
    from blog_pipeline.graphs.runner import resume_article

    result = resume_article(article_id, "reject", note)
    _print_run_result(result)


@app.command("status")
def status_cmd() -> None:
    """Show pipeline health metrics."""
    from blog_pipeline.metrics import gather_metrics

    m = gather_metrics()
    table = Table("Metric", "Value")
    for k, v in m.items():
        table.add_row(k, str(v))
    console.print(table)
    if m["coverage_weeks"] < 1.0:
        console.print("[red]⚠ Calendar coverage below 1 week — run run-calendar.[/red]")


@app.command("calendar")
def calendar_cmd(limit: int = typer.Option(20, "--limit")) -> None:
    """Show the upcoming scheduled queue."""
    with get_session() as s:
        rows = (
            s.query(CalendarEntry)
            .filter(CalendarEntry.status == EntryStatus.queued)
            .order_by(CalendarEntry.scheduled_date)
            .limit(limit)
            .all()
        )
        if not rows:
            console.print("[dim]Calendar is empty. Run run-calendar.[/dim]")
            return
        table = Table("Date", "Topic", "Keywords", "Source")
        for e in rows:
            table.add_row(
                e.scheduled_date.isoformat(), e.topic[:50],
                ", ".join(e.target_keywords or [])[:40], e.source.value,
            )
        console.print(table)


@app.command("config-check")
def config_check_cmd() -> None:
    """Show which integrations are configured (no secrets printed)."""
    s = get_settings()
    table = Table("Integration", "Configured")
    table.add_row("OpenRouter (LLM)", "✓" if s.has_openrouter else "✗ (required)")
    table.add_row("Shopify", "✓" if s.has_shopify else "✗ (required to publish)")
    table.add_row("Image gen (fal.ai)", "✓" if s.has_images else "— (skipped)")
    table.add_row("DataForSEO", "✓" if s.has_dataforseo else "— (LLM-only research)")
    table.add_row("Slack", "✓" if s.has_slack else "— (logs to stdout)")
    table.add_row("LangSmith", "✓" if s.langsmith_api_key else "— (no tracing)")
    table.add_row("Gate mode", s.gate_mode)
    console.print(table)


def _print_run_result(result: dict) -> None:
    status = result.get("status")
    if status == "pending_review":
        console.print(
            f"[yellow]⏸ Pending human review[/yellow] "
            f"(article thread {result.get('thread_id')})."
        )
        _notify_pending(result)
    elif status == "published":
        console.print(f"[green]✓ Published[/green]: {result.get('result')}")
    elif status == "dry_run":
        console.print("[cyan]Dry run — would publish:[/cyan]")
        console.print_json(json.dumps(result.get("result"), default=str))
    elif status in ("rejected", "blocked", "failed"):
        console.print(f"[red]✗ {status}[/red]: {result.get('result')}")
    else:
        console.print_json(json.dumps(result, default=str))


def _notify_pending(result: dict) -> None:
    """Fire the Slack approval request for a just-paused article."""
    from blog_pipeline.notify import send_approval_request

    interrupt = result.get("interrupt") or {}
    payload = interrupt if isinstance(interrupt, dict) else {}
    # interrupt value is nested under the langgraph interrupt structure
    data = payload.get("value", payload) if isinstance(payload, dict) else {}
    article_id = data.get("article_id")
    if article_id:
        send_approval_request(
            article_id, data.get("title", ""), data.get("seo_score"),
            data.get("confidence"), None,
        )


if __name__ == "__main__":
    app()
