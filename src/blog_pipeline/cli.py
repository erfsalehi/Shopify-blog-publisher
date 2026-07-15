"""Command-line entry point (Typer).

Commands map to the pipeline's operational surface:
  init-db       create tables
  run-article   draft one topic now and sync it to Linear (supports --dry-run)
  run-calendar  weekly topic-queue refresh (Content Calendar agent)
  run-daily     draft every calendar entry due today
  add-topic     manually queue a topic on a date
  status        health metrics dashboard
  calendar      show the upcoming scheduled queue

Review and publishing happen in Linear, not here — every drafted article ends
up as a fully-populated Linear issue for a human to check and publish by hand.
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
    CalendarEntry,
    EntryStatus,
    TopicSource,
)

app = typer.Typer(add_completion=False, help="Blog Topic Research → Linear pipeline")
console = Console()


@app.command("init-db")
def init_db_cmd() -> None:
    """Create database tables and reconcile enum labels."""
    added = _init_db()
    console.print("[green]Database initialized.[/green]")
    if added:
        # Worth reporting: it means the schema had drifted from the code, and
        # every insert using these labels was failing until now.
        console.print(f"[yellow]Added missing enum labels: {', '.join(added)}[/yellow]")


@app.command("serve")
def serve_cmd(
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Run the WhatsApp webhook server (trigger the pipeline by message).

    Requires the [whatsapp] extra: pip install -e ".[whatsapp]".
    Point your Meta app's webhook callback at https://<host>/webhook.
    """
    try:
        import uvicorn
    except ImportError:
        console.print("[red]Missing deps.[/red] Install with: pip install -e \".[whatsapp]\"")
        raise typer.Exit(1)
    from blog_pipeline.webhook import create_app

    _init_db()
    console.print(f"[bold]WhatsApp webhook[/bold] on http://{host}:{port}/webhook")
    uvicorn.run(create_app(), host=host, port=port)


@app.command("run-article")
def run_article_cmd(
    topic: str = typer.Option(..., "--topic", "-t", help="Article topic"),
    keywords: str = typer.Option("", "--keywords", "-k", help="Comma-separated"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Don't sync to Linear; print payload"),
) -> None:
    """Draft a single article from a manual topic and sync it to Linear."""
    from blog_pipeline.graphs.runner import run_article

    _init_db()
    kw = [k.strip() for k in keywords.split(",") if k.strip()]
    console.print(f"[bold]Running article pipeline[/bold] for: {topic}")
    result = run_article(topic, kw, dry_run=dry_run, source=TopicSource.manual)
    _print_run_result(result)
    if not dry_run:
        _notify_article(topic, result)


@app.command("run-calendar")
def run_calendar_cmd(
    niche: str = typer.Option("", "--niche"),
    seeds: str = typer.Option(
        "", "--seeds",
        help="Comma-separated seed keywords. Omit to auto-research them from --niche.",
    ),
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
    """Draft + sync to Linear every calendar entry due today (Daily trigger)."""
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
        if not dry_run:
            _notify_article(entry["topic"], result)


@app.command("add-topic")
def add_topic_cmd(
    topic: str = typer.Option(..., "--topic", "-t"),
    on: str = typer.Option(..., "--on", help="Scheduled date YYYY-MM-DD"),
    keywords: str = typer.Option("", "--keywords", "-k"),
) -> None:
    """Manually add a topic to the calendar on a given date, syncing it to
    Linear as a Backlog issue like an auto-researched topic would be."""
    from blog_pipeline.config import get_settings
    from blog_pipeline.graphs.calendar_graph import _get_or_create_calendar
    from blog_pipeline.tools.linear import LinearClient, LinearError

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
        if get_settings().has_linear:
            try:
                client = LinearClient()
                result = client.create_issue(
                    title=topic,
                    description=f"**Target keywords:** {', '.join(kw)}" if kw else None,
                    state="Backlog", due_date=sched.isoformat(), labels=["Blog"],
                )
                entry.linear_issue_id = result.id
                entry.linear_identifier = result.identifier
                entry.linear_url = result.url
                client.close()
            except LinearError as e:
                console.print(f"[yellow]Linear sync failed:[/yellow] {e}")
    console.print(f"[green]Queued[/green] '{topic}' for {sched}.")


@app.command("import-existing")
def import_existing_cmd(
    limit: int = typer.Option(250, "--limit", help="Max posts to pull."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be imported, write nothing."
    ),
) -> None:
    """Import the store's existing Shopify posts so dedup can see them.

    Both duplicate checks compare only against this database, so a blog that
    predates the pipeline is invisible: research re-proposes topics you
    published years ago. Idempotent — safe to re-run.
    """
    from blog_pipeline.backfill import import_shopify_articles

    s = get_settings()
    if not s.has_shopify:
        # Shopify is an optional integration, so an unconfigured store is a
        # no-op rather than an error — a store-less run just dedupes against
        # whatever the pipeline wrote itself. A configured-but-broken store
        # still raises from the client, which is the case worth shouting about.
        console.print("[yellow]Shopify not configured — nothing to import. "
                      "Set SHOPIFY_STORE_DOMAIN and SHOPIFY_ACCESS_TOKEN to "
                      "dedupe against your existing posts.[/yellow]")
        return

    result = import_shopify_articles(limit=limit, dry_run=dry_run)
    table = Table("Metric", "Value")
    for k, v in result.items():
        table.add_row(k, str(v))
    console.print(table)
    if dry_run:
        console.print("[dim]Dry run — nothing written.[/dim]")


@app.command("sync-performance")
def sync_performance_cmd(
    days: int = typer.Option(90, "--days", help="Window length to pull."),
    list_sites: bool = typer.Option(
        False, "--list-sites",
        help="Show properties the service account can read, then exit.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fetch but don't store."),
) -> None:
    """Pull Search Console performance into the database.

    Run it on a schedule: comparing two windows is what reveals decay, and one
    window on its own can't show a trend.
    """
    from blog_pipeline.performance import sync_performance
    from blog_pipeline.tools.search_console import (
        SearchConsoleClient,
        SearchConsoleError,
    )

    if list_sites:
        # The setup diagnostic. An empty list means the key authenticates but
        # was never granted access to the property — the usual first failure.
        try:
            sites = SearchConsoleClient().list_sites()
        except SearchConsoleError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        if not sites:
            console.print(
                "[yellow]The service account can't see any properties. Add its "
                "client_email as a user in Search Console → Settings → Users "
                "and permissions.[/yellow]"
            )
            return
        table = Table("Property", "Permission")
        for s in sites:
            table.add_row(s.get("siteUrl", "?"), s.get("permissionLevel", "?"))
        console.print(table)
        console.print(f"[dim]Configured as: {get_settings().gsc_property}[/dim]")
        return

    try:
        result = sync_performance(days=days, dry_run=dry_run)
    except SearchConsoleError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if not result.get("enabled"):
        console.print(
            "[yellow]Search Console not configured — set GSC_CREDENTIALS_JSON "
            "(and GSC_SITE_URL if it isn't sc-domain:<your public domain>).[/yellow]"
        )
        return

    table = Table("Metric", "Value")
    for k, v in result.items():
        table.add_row(k, str(v))
    console.print(table)
    if result["pages"] and not result["matched"]:
        console.print(
            "[yellow]⚠ No pages matched a known article. Run import-existing "
            "first, and check PUBLIC_DOMAIN matches the property.[/yellow]"
        )


@app.command("run-refresh")
def run_refresh_cmd(
    older_than_months: int = typer.Option(
        12, "--older-than-months", help="Only consider posts older than this."
    ),
    limit: int = typer.Option(5, "--limit", help="Max posts to refresh in one run."),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually write to Shopify. Without this, reports only.",
    ),
) -> None:
    """Refresh stale live posts, oldest first.

    Writes to LIVE pages: Shopify has no draft revision for a published post,
    so --apply edits public content immediately. The previous body is
    snapshotted first — `rollback-refresh` restores it.
    """
    from blog_pipeline.graphs.refresh_graph import run_refresh

    s = get_settings()
    if not s.has_shopify:
        console.print("[red]Shopify not configured — nothing to refresh.[/red]")
        raise typer.Exit(1)

    result = run_refresh(
        older_than_months=older_than_months, limit=limit, dry_run=not apply
    )
    table = Table("Article", "Outcome", "Notes")
    for a in result["articles"]:
        notes = a.get("error") or "; ".join(a.get("changes") or [])
        table.add_row(str(a["title"])[:45], a["outcome"], notes[:60] or "—")
    console.print(table)
    console.print(
        f"considered={result['considered']} refreshed={result['refreshed']} "
        f"skipped={result['skipped']} failed={result['failed']} "
        f"cost=${result['cost_usd']}"
    )
    if result["dry_run"]:
        console.print("[dim]Dry run — Shopify untouched. Re-run with --apply.[/dim]")


@app.command("rollback-refresh")
def rollback_refresh_cmd(
    article_id: int = typer.Argument(..., help="Article id to restore."),
    apply: bool = typer.Option(False, "--apply", help="Actually write to Shopify."),
) -> None:
    """Restore an article's pre-refresh body to Shopify."""
    from blog_pipeline.backfill import rollback_refresh

    try:
        result = rollback_refresh(article_id, dry_run=not apply)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(
        f"Restored article {result['article_id']} from snapshot taken "
        f"{result['restored_from']}."
    )
    if result["dry_run"]:
        console.print("[dim]Dry run — Shopify untouched. Re-run with --apply.[/dim]")


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
        table = Table("Date", "Topic", "Keywords", "Source", "Linear")
        for e in rows:
            table.add_row(
                e.scheduled_date.isoformat(), e.topic[:50],
                ", ".join(e.target_keywords or [])[:40], e.source.value,
                e.linear_identifier or "—",
            )
        console.print(table)


@app.command("config-check")
def config_check_cmd() -> None:
    """Show which integrations are configured (no secrets printed)."""
    s = get_settings()
    table = Table("Integration", "Configured")
    table.add_row("Google AI Studio (LLM)", "✓" if s.has_google else "✗ (required)")
    table.add_row("Linear", "✓" if s.has_linear else "✗ (required to sync drafts)")
    if s.has_shopify:
        shopify_status = "✓ (auto-publish on)" if s.can_autopublish else "✓ (publish disabled)"
    else:
        shopify_status = "— (Linear-only; no auto-publish)"
    table.add_row("Shopify", shopify_status)
    table.add_row(
        "Image gen (OpenRouter)",
        "✓" if s.has_images else ("— (disabled)" if not s.enable_images else "— (no key)"),
    )
    table.add_row("DataForSEO", "✓" if s.has_dataforseo else "— (LLM-only research)")
    table.add_row("Slack", "✓" if s.has_slack else "— (logs to stdout)")
    table.add_row("WhatsApp (Meta)", "✓" if s.has_whatsapp else "— (no trigger webhook)")
    table.add_row("LangSmith", "✓" if s.langsmith_api_key else "— (no tracing)")
    console.print(table)


def _print_run_result(result: dict) -> None:
    status = result.get("status")
    if status == "published":
        console.print(f"[green]✓ Published live to Shopify[/green]: {result.get('result')}")
    elif status == "synced":
        console.print(f"[green]✓ Synced to Linear[/green]: {result.get('result')}")
    elif status == "dry_run":
        console.print("[cyan]Dry run — would sync to Linear (and publish if configured):[/cyan]")
        console.print_json(json.dumps(result.get("result"), default=str))
    elif status == "failed":
        console.print(f"[red]✗ failed[/red]: {result.get('result')}")
    else:
        console.print_json(json.dumps(result, default=str))


def _notify_article(topic: str, result: dict) -> None:
    """Post the per-article outcome to Slack (no-op if Slack unconfigured)."""
    from blog_pipeline.notify import send_article_update

    try:
        send_article_update(topic, result)
    except Exception:
        pass


if __name__ == "__main__":
    app()
