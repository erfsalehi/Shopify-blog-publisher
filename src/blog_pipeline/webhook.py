"""FastAPI webhook that lets you trigger the pipeline from WhatsApp.

Run it with `blog-pipeline serve` (needs the [whatsapp] extra installed) and
point your Meta app's webhook callback URL at `https://<host>/webhook`.

Flow:
  GET  /webhook  -> Meta's subscribe handshake (echo hub.challenge).
  POST /webhook  -> verify signature, allow-list the sender, parse the command,
                    ACK immediately, then run the pipeline in the background and
                    message the result back over WhatsApp.

The pipeline call is slow (LLM drafting), so it runs in a background task while
the webhook returns 200 fast — Meta retries if you're slow to respond.
"""

from __future__ import annotations

from blog_pipeline.tools.whatsapp import (
    HELP_TEXT,
    Command,
    extract_messages,
    is_allowed,
    notify,
    parse_command,
    verify_challenge,
    verify_signature,
)


# ── command -> pipeline (pure enough to unit-test) ────────────────
def run_command(cmd: Command) -> str:
    """Execute a parsed command and return a WhatsApp-ready result string."""
    from blog_pipeline.db import init_db
    from blog_pipeline.db.models import TopicSource

    init_db()

    if cmd.action == "help":
        return HELP_TEXT
    if cmd.action == "status":
        from blog_pipeline.metrics import gather_metrics

        m = gather_metrics()
        return (
            "📊 *Pipeline status*\n"
            f"• Published: {m.get('articles_published', 0)}  ·  "
            f"Synced: {m.get('articles_synced', 0)}  ·  Failed: {m.get('articles_failed', 0)}\n"
            f"• Avg SEO: {m.get('avg_seo_score')}  ·  Coverage: {m.get('coverage_weeks')} wks"
        )
    if cmd.action == "draft":
        from blog_pipeline.graphs.runner import run_article

        r = run_article(cmd.arg, [], source=TopicSource.manual)
        return _format_article(cmd.arg, r)
    if cmd.action == "daily":
        from blog_pipeline.graphs.calendar_graph import get_due_entries
        from blog_pipeline.graphs.runner import run_article

        due = get_due_entries()
        if not due:
            return "📅 Nothing due today — no-op."
        lines = [f"📅 Drafting {len(due)} due article(s)…"]
        for e in due:
            r = run_article(e["topic"], e["target_keywords"],
                            source=TopicSource.auto_researched, entry_id=e["id"])
            lines.append(_format_article(e["topic"], r))
        return "\n\n".join(lines)
    if cmd.action == "calendar":
        from blog_pipeline.graphs.calendar_graph import run_calendar

        res = run_calendar()
        return (f"🗓️ Calendar: added {res.get('added', 0)} topics · "
                f"{res.get('coverage_weeks')} wks coverage ({res.get('status')}).")
    return "🤷 Didn't recognize that.\n\n" + HELP_TEXT


def _format_article(topic: str, result: dict) -> str:
    status = result.get("status")
    r = result.get("result") or {}
    linear = r.get("linear_identifier")
    if status == "published":
        return f"✅ *Published:* {topic}\n{r.get('shopify_url')}"
    if status == "synced":
        extra = " · Shopify draft created" if r.get("shopify_article_id") else ""
        return (f"📝 *Drafted:* {topic}\nLinear {linear} → "
                f"{r.get('linear_state', 'synced')}{extra}\n{r.get('url', '')}")
    if status == "failed":
        return f"⚠️ *Failed:* {topic} — {r.get('error') or r}"
    return f"{topic}: {status}"


def _process_and_reply(to: str, text: str) -> None:
    """Background worker: run the command and message the outcome back."""
    cmd = parse_command(text)
    try:
        reply = run_command(cmd)
    except Exception as e:  # never let a run crash the worker silently
        reply = f"⚠️ Error running that: {e}"
    notify(to, reply)


# ── FastAPI app ──────────────────────────────────────────────────
def create_app():
    from fastapi import BackgroundTasks, FastAPI, Request, Response

    app = FastAPI(title="Blog pipeline WhatsApp webhook")

    @app.get("/webhook")
    def verify(request: Request):
        q = request.query_params
        challenge = verify_challenge(
            q.get("hub.mode", ""), q.get("hub.verify_token", ""), q.get("hub.challenge", "")
        )
        if challenge is None:
            return Response(status_code=403, content="verification failed")
        return Response(status_code=200, content=challenge, media_type="text/plain")

    @app.post("/webhook")
    async def receive(request: Request, background: BackgroundTasks):
        raw = await request.body()
        if not verify_signature(raw, request.headers.get("x-hub-signature-256")):
            return Response(status_code=403, content="bad signature")

        payload = await request.json()
        for msg in extract_messages(payload):
            if not is_allowed(msg.from_number):
                # Silently ignore strangers; optionally ack the owner elsewhere.
                continue
            # Immediate ack, then do the slow work in the background.
            notify(msg.from_number, "🛠️ On it…")
            background.add_task(_process_and_reply, msg.from_number, msg.text)
        # Always 200 quickly so Meta doesn't retry.
        return Response(status_code=200, content="ok")

    @app.get("/health")
    def health():
        return {"ok": True}

    return app
