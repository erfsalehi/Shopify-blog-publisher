"""The FastAPI app: routes, template wiring, lifespan.

Every handler here reads the database and nothing else. If a route ever needs
to call Shopify or Google directly, that's a job that hasn't been written yet
— see the rule at the top of `dashboard/__init__.py`.

No authentication, by decision: the app binds to loopback and is not reachable
from anywhere else. The corollary is that it must never be bound to 0.0.0.0.
"""

from __future__ import annotations

import logging
import re
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from dashboard import (
    advisor, alerts, auth, charts, cron, diffing, experiments, refresh,
    reporting, scheduler, store, strategy,
)
from blog_pipeline.tools.shopify import ShopifyClient, ShopifyError

from dashboard.config import get_settings, is_serverless, pipeline
from dashboard.db import init_db, init_pipeline_db
from dashboard.jobs import all_jobs
from dashboard.jobs.gsc import SETTLING_DAYS
from dashboard.jobs.registry import get_job
from dashboard.jobs.runner import (
    JobAlreadyRunning,
    is_running,
    last_runs,
    reap_stale_runs,
    recent_runs,
    run_in_background,
    run_job,
)
from dashboard.models import (
    AlertRule, Competitor, CompetitorMatch, CompetitorPost, Experiment,
    ExperimentProduct, MatchStatus, ShopifyProduct, Strategy,
    StrategyCheckpoint, StrategyStep,
)
from dashboard.db import get_session

log = logging.getLogger(__name__)

_HERE = Path(__file__).parent
TEMPLATES = _HERE / "templates"
STATIC = _HERE / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # No-op locally, where the pipeline has its own database and its own
    # `blog-pipeline init-db`. On Vercel both share the one Neon database and
    # that CLI can't be run, so the Blog page's reads of the pipeline's
    # `article` table would fail until this creates it.
    init_pipeline_db()
    # Close out any run whose process died mid-job, so the Jobs page doesn't
    # show something permanently in flight that nothing will ever finish.
    reap_stale_runs()
    if get_settings().enable_scheduler_effective:
        scheduler.start()
    yield
    scheduler.shutdown()


def _fmt_int(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def _fmt_pct(value, places: int = 2) -> str:
    try:
        return f"{float(value):.{places}f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_num(value, places: int = 1) -> str:
    try:
        return f"{float(value):.{places}f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_when(value) -> str:
    """Timestamps in the machine's own local time.

    Relative times ("3h ago") read nicely and are useless for the question
    actually being asked on the jobs page, which is "did the 6am run happen
    this morning". Answering it in UTC is worse than useless — the owner sees
    09:05 for a job they watched run at 12:36 and concludes the log is broken.

    Rows are stored in UTC and come back from SQLite without a tzinfo, so a
    naive value here is UTC by construction, not by guess.
    """
    if value is None:
        return "never"
    if isinstance(value, datetime):
        aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        return aware.astimezone().strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _query_string(**params) -> str:
    """Filter state as a query string, for pagination and sort links.

    Empty values are dropped so a bare "/products" doesn't turn into a URL
    carrying six empty parameters after one click.
    """
    from urllib.parse import urlencode

    return urlencode({k: v for k, v in params.items() if v not in ("", None)})


def _fmt_money(value, currency: str = "$") -> str:
    try:
        return f"{currency}{float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _md(text: str) -> Markup:
    """Render the advisor's short markdown safely.

    Escapes first, then applies a handful of inline patterns. A full markdown
    library would be a dependency added for four regexes, and — more to the
    point — one that renders raw HTML, which is the last thing to allow on
    text a language model produced.
    """
    if not text:
        return Markup("")
    out = str(escape(text))
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<em>\1</em>", out)
    out = re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", out)
    blocks = []
    for chunk in re.split(r"\n\s*\n", out.strip()):
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        if lines and all(re.match(r"^[-*]\s+", ln) for ln in lines):
            items = "".join(
                "<li>" + re.sub(r"^[-*]\s+", "", ln) + "</li>" for ln in lines
            )
            blocks.append("<ul>" + items + "</ul>")
        else:
            blocks.append("<p>" + "<br>".join(lines) + "</p>")
    return Markup("".join(blocks))


def _fmt_duration(ms) -> str:
    if ms is None:
        return "—"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60)}s"


# Preview runs are slow (an LLM call per article) and must not block the
# request thread, but they are also not scheduled work — they belong to the
# person who clicked. So they get their own small thread registry rather than
# going through the job runner, which exists for recurring syncs.
_PREVIEWS: dict[int, threading.Thread] = {}
_PREVIEWS_GUARD = threading.Lock()


def _preview_running(article_id: int) -> bool:
    with _PREVIEWS_GUARD:
        thread = _PREVIEWS.get(article_id)
    return bool(thread and thread.is_alive())


# Advisor generation is a reasoning-model call — slow enough that holding the
# request open for it would look like a hung page.
_ADVICE: dict[str, threading.Thread] = {}
_ADVICE_GUARD = threading.Lock()

# Which page each advisor scope lives on, so a generate can redirect back.
_SCOPE_PATHS = {
    "overview": "", "products": "products", "blog": "blog",
    "keywords": "keywords", "ads": "ads", "experiments": "experiments",
    "competitors": "competitors", "local": "local",
}


def _scope_path(scope: str) -> str:
    return _SCOPE_PATHS.get(scope, "")


def _advice_running(scope: str) -> bool:
    with _ADVICE_GUARD:
        thread = _ADVICE.get(scope)
    return bool(thread and thread.is_alive())


def _start_advice(scope: str) -> None:
    def work() -> None:
        try:
            advisor.generate(scope)
        except Exception:  # noqa: BLE001
            log.exception("advisor generation for %s crashed", scope)

    thread = threading.Thread(target=work, name=f"advice-{scope}", daemon=True)
    with _ADVICE_GUARD:
        _ADVICE[scope] = thread
    thread.start()


def _last_dfs_error() -> str | None:
    """Why the last keyword job didn't return data, if it didn't.

    Read from the job log rather than re-calling: an unverified DataForSEO
    account is a standing condition, and the Keywords page should say so
    without spending a request to rediscover it.
    """
    run = last_runs().get("dataforseo_keywords")
    if run is None or run.status != "skipped":
        return None
    return run.error


def _start_preview(article_id: int) -> None:
    def work() -> None:
        try:
            refresh.preview(article_id)
        except Exception:  # noqa: BLE001
            # preview() records its own failures as a proposal row; this is
            # for anything that escapes before it gets that far.
            log.exception("refresh preview for article %s crashed", article_id)

    thread = threading.Thread(
        target=work, name=f"preview-{article_id}", daemon=True
    )
    with _PREVIEWS_GUARD:
        _PREVIEWS[article_id] = thread
    thread.start()


def create_app() -> FastAPI:
    app = FastAPI(title="D&R Flooring Control Center", lifespan=lifespan)
    # Order matters, and it's the opposite of what add-order suggests: the
    # LAST middleware added ends up OUTERMOST (Starlette builds the stack by
    # wrapping in reverse of registration order), so the guard has to be
    # added first and the session second for the session to actually be
    # available by the time the guard's dispatch function runs.
    # `test_every_page_redirects_to_login_when_a_password_is_set` and its
    # siblings pin this the way that actually matters — by exercising the
    # wired-up app over HTTP, not by asserting the call order in prose.
    app.middleware("http")(auth.auth_guard)
    auth.install_session_middleware(app)
    # /api/cron/* — its own bearer-token check in cron.py, deliberately not
    # the password login: Vercel Cron has no browser to carry the session
    # cookie. auth.py's _PUBLIC_PATHS already excludes this prefix from the
    # guard above, so registering it here doesn't also require a login.
    app.include_router(cron.router)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.filters.update(
        int_=_fmt_int, pct=_fmt_pct, num=_fmt_num, when=_fmt_when,
        duration=_fmt_duration, money=_fmt_money, md=_md,
    )

    # ── Auth ────────────────────────────────────────────────────────
    # Registered before anything else in the function only for reading
    # order — the auth_guard middleware, not route order, is what actually
    # makes these reachable when every other page redirects here.
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/", error: str = ""):
        if not get_settings().auth_required or auth.is_authenticated(request):
            return RedirectResponse(next or "/", status_code=303)
        return templates.TemplateResponse(
            request, "login.html", {"next": next, "error": error}
        )

    @app.post("/login")
    async def login_submit(request: Request):
        form = await request.form()
        password = str(form.get("password") or "")
        next_url = str(form.get("next") or "/")
        # Never redirect off-site with a value taken from the request — an
        # open redirect turns this login form into a phishing launchpad.
        if not next_url.startswith("/") or next_url.startswith("//"):
            next_url = "/"

        client_ip = request.client.host if request.client else "unknown"
        if auth.is_locked_out(client_ip):
            return templates.TemplateResponse(
                request, "login.html",
                {"next": next_url,
                 "error": "Too many attempts. Wait a few minutes and try again."},
                status_code=429,
            )
        if not auth.check_password(password):
            auth.record_failure(client_ip)
            return templates.TemplateResponse(
                request, "login.html",
                {"next": next_url, "error": "Wrong password."},
                status_code=401,
            )
        request.session["authenticated"] = True
        return RedirectResponse(next_url, status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    def render(request: Request, name: str, **context) -> HTMLResponse:
        context.setdefault("nav", name.split(".")[0])
        context.setdefault("coverage", reporting.coverage())
        scope = context.get("advisor_scope")
        if scope:
            context.setdefault("advisor_note", advisor.latest_note(scope))
            context.setdefault("advisor_open", advisor.actions_for(scope))
            context.setdefault("advisor_history", [
                a for a in advisor.actions_for(scope, include_resolved=True)
                if a.status != "open"
            ][:15])
            context.setdefault("advisor_back", str(request.url.path))
        # The open-alert count rides on every page: an alerting system you have
        # to remember to go and look at is one you stop looking at.
        context.setdefault("open_alerts", alerts.open_count())
        context.setdefault("auth_required", get_settings().auth_required)
        return templates.TemplateResponse(request, name, context)

    # ── Overview ────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request, window: int = 28, days: int = 90):
        window = max(7, min(window, 180))
        days = max(14, min(days, 480))
        summary = reporting.site_summary(window_days=window)
        series = reporting.site_series(days=days)
        clicks_chart = charts.line_chart(
            [
                charts.Point(
                    label=p.day.isoformat(),
                    value=p.clicks,
                    provisional=p.provisional,
                    tooltip=(
                        f"{p.day.isoformat()}: {p.clicks:,} clicks, "
                        f"{p.impressions:,} impressions"
                    ),
                )
                for p in series
            ],
            title="Clicks per day",
            empty_note="No Search Console data yet — run the sync on the Jobs page.",
        )
        impressions_chart = charts.line_chart(
            [
                charts.Point(
                    label=p.day.isoformat(),
                    value=p.impressions,
                    provisional=p.provisional,
                    tooltip=f"{p.day.isoformat()}: {p.impressions:,} impressions",
                )
                for p in series
            ],
            title="Impressions per day",
            empty_note="No Search Console data yet.",
        )
        return render(
            request,
            "overview.html",
            summary=summary,
            window=window,
            days=days,
            clicks_chart=clicks_chart,
            impressions_chart=impressions_chart,
            pages=reporting.top_pages(window_days=window, limit=25),
            advisor_scope="overview",
            last_sync=last_runs().get("gsc_daily"),
            settling_days=SETTLING_DAYS,
            backfill_days=store.get(store.GSC_BACKFILL_DAYS),
        )

    # ── Products ────────────────────────────────────────────────────
    @app.get("/products", response_class=HTMLResponse)
    def products_page(
        request: Request,
        window: int = 28,
        q: str = "",
        status: str = "",
        priced: str = "",
        experiment: str = "",
        cohort: str = "",
        order: str = "clicks",
        page: int = 1,
    ):
        window = max(7, min(window, 180))
        per_page = 50
        page = max(1, page)
        data = reporting.products(
            window_days=window, search=q, status=status, priced=priced,
            experiment=experiment, cohort=cohort, order=order,
            limit=per_page, offset=(page - 1) * per_page,
        )
        pages = max(1, -(-data["total"] // per_page))
        return render(
            request, "products.html",
            data=data, window=window, q=q, status=status, priced=priced,
            experiment=experiment, cohort=cohort, order=order,
            page=page, pages=pages,
            advisor_scope="products",
            query_base=_query_string(
                window=window, q=q, status=status, priced=priced,
                experiment=experiment, cohort=cohort, order=order,
            ),
        )

    # ── Blog ────────────────────────────────────────────────────────
    @app.get("/blog", response_class=HTMLResponse)
    def blog_page(request: Request, window: int = 28, order: str = "decay"):
        window = max(7, min(window, 180))
        pipeline_state = reporting.content_pipeline()
        return render(
            request, "blog.html",
            data=reporting.blog_posts(window_days=window, order=order),
            pipeline=pipeline_state,
            funnel=(
                charts.pipeline_funnel(
                    pipeline_state["counts"], pipeline_state["stages"]
                )
                if pipeline_state["available"] else ""
            ),
            advisor_scope="blog",
            window=window, order=order,
            last_sync=last_runs().get("blog_articles"),
        )

    @app.get("/blog/{article_id}", response_class=HTMLResponse)
    def article_page(request: Request, article_id: int, window: int = 28):
        window = max(7, min(window, 180))
        detail = reporting.article_detail(article_id, window_days=window)
        if detail is None:
            return HTMLResponse("Article not found", status_code=404)
        proposal = refresh.latest_proposal(article_id)
        diff_lines = summary = None
        if proposal is not None and proposal.proposed_html:
            diff_lines = diffing.collapse(
                diffing.diff(proposal.original_html, proposal.proposed_html)
            )
            summary = diffing.summarise(
                proposal.original_html, proposal.proposed_html
            )
        return render(
            request, "article.html",
            nav="blog",
            detail=detail, window=window,
            proposal=proposal, diff_lines=diff_lines, diff_summary=summary,
            history=refresh.proposals_for(article_id),
            revisions=refresh.revisions_for(article_id),
            running=_preview_running(article_id),
        )

    @app.post("/blog/{article_id}/preview")
    def preview_refresh(article_id: int):
        """Kick off a dry run. Writes nothing to Shopify."""
        if _preview_running(article_id):
            return JSONResponse(
                {"started": False, "reason": "already running"}, status_code=409
            )
        _start_preview(article_id)
        return JSONResponse({"started": True, "article_id": article_id})

    @app.get("/blog/{article_id}/preview/status")
    def preview_status(article_id: int):
        proposal = refresh.latest_proposal(article_id)
        return {
            "running": _preview_running(article_id),
            "status": proposal.status if proposal else None,
        }

    @app.post("/blog/{article_id}/apply")
    def apply_refresh(article_id: int, proposal_id: int = Form(...)):
        """Publish an approved proposal. The one route here that changes a
        public page, and it only ever runs because a person clicked approve
        on a diff — nothing schedules it."""
        try:
            refresh.apply(proposal_id)
        except (refresh.RefreshRefused, ShopifyError) as exc:
            return RedirectResponse(
                f"/blog/{article_id}?error={quote(str(exc)[:400])}",
                status_code=303,
            )
        return RedirectResponse(f"/blog/{article_id}?applied=1", status_code=303)

    # ── Keywords ────────────────────────────────────────────────────
    @app.get("/keywords", response_class=HTMLResponse)
    def keywords_page(
        request: Request,
        window: int = 28,
        q: str = "",
        order: str = "opportunity",
        striking: int = 1,
    ):
        window = max(7, min(window, 180))
        data = reporting.keywords(
            window_days=window, order=order, striking_only=bool(striking),
            search=q, limit=150,
        )
        return render(
            request, "keywords.html",
            data=data, window=window, q=q, order=order,
            striking=bool(striking),
            dfs_error=_last_dfs_error(),
            advisor_scope="keywords",
        )

    def _sync_topic_to_linear(entry, topic: str, keywords: list[str], notes: str):
        """Mirror a queued topic into Linear, as `add-topic` does.

        Review and publishing happen in Linear, not here — a topic queued
        only in the database would be invisible to the process that actually
        acts on it. Failure is non-fatal for the same reason it is in the
        CLI: the entry is already saved, and losing the queue because an
        unrelated API is down would be the worse outcome.
        """
        from blog_pipeline.tools.linear import LinearClient, LinearError

        if not pipeline().has_linear:
            return
        description = f"Countering a competitor post.\n\n{notes}"
        if keywords:
            description += f"\n\n**Target keywords:** {', '.join(keywords)}"
        try:
            client = LinearClient()
            result = client.create_issue(
                title=topic,
                description=description,
                state="Backlog",
                due_date=date.today().isoformat(),
                labels=["Blog"],
            )
            entry.linear_issue_id = result.id
            entry.linear_identifier = result.identifier
            entry.linear_url = result.url
            client.close()
        except LinearError:
            log.exception("Linear sync failed for countered topic %r", topic)

    @app.post("/blog/{article_id}/send-to-shopify")
    def send_draft_to_shopify(article_id: int):
        """Create the article in Shopify as an UNPUBLISHED draft.

        For articles QA held back: they have a finished body sitting in the
        database and no presence in Shopify at all, so there is nowhere to
        review them except a Linear description. This puts the real thing in
        the real editor, still hidden, and the publish decision stays a human
        click in Shopify admin.

        `published=False` always, regardless of SHOPIFY_PUBLISH_LIVE. That
        setting governs what the automated pipeline may do on its own; this
        is a person asking for a draft, and answering it by publishing live
        would be the single worst way to misread the request.
        """
        from blog_pipeline.db.models import Article as PipelineArticle
        from blog_pipeline.db.session import get_session as pipeline_session

        with pipeline_session() as session:
            row = session.get(PipelineArticle, article_id)
            if row is None:
                return JSONResponse({"error": "unknown article"}, status_code=404)
            if row.shopify_article_id:
                # Already there. Never create a second copy of a post.
                return RedirectResponse(
                    f"/blog/{article_id}?already=1", status_code=303
                )
            if not (row.draft_html or "").strip():
                return RedirectResponse(
                    f"/blog/{article_id}?nobody=1", status_code=303
                )
            payload = {
                "title": row.title or row.topic,
                "body_html": row.draft_html,
                "summary": row.seo_description,
                "seo_title": row.seo_title,
                "seo_description": row.seo_description,
                "handle": row.handle,
            }

        try:
            client = ShopifyClient()
            try:
                result = client.create_article(published=False, **payload)
            finally:
                client.close()
        except ShopifyError as e:
            log.exception("sending article %s to Shopify failed", article_id)
            return RedirectResponse(
                f"/blog/{article_id}?error={quote(str(e)[:200])}", status_code=303
            )

        with pipeline_session() as session:
            row = session.get(PipelineArticle, article_id)
            if row is not None:
                row.shopify_article_id = result.article_id
                row.shopify_url = result.url
        return RedirectResponse(f"/blog/{article_id}?drafted=1", status_code=303)

    # ── Strategy ────────────────────────────────────────────────────
    @app.get("/strategy", response_class=HTMLResponse)
    def strategy_page(request: Request):
        with get_session() as session:
            rows = (
                session.query(Strategy)
                .order_by(Strategy.created_at.desc())
                .limit(30)
                .all()
            )
            out = []
            for row in rows:
                steps = session.query(StrategyStep).filter(
                    StrategyStep.strategy_id == row.id
                ).all()
                latest = (
                    session.query(StrategyCheckpoint)
                    .filter(StrategyCheckpoint.strategy_id == row.id)
                    .order_by(StrategyCheckpoint.date.desc())
                    .first()
                )
                out.append({
                    "strategy": row,
                    "total": len(steps),
                    "done": sum(1 for s in steps if s.status == "done"),
                    "latest": latest,
                })
            for item in out:
                session.expunge(item["strategy"])
                if item["latest"] is not None:
                    session.expunge(item["latest"])
        return render(request, "strategy.html", strategies=out)

    @app.post("/strategy")
    def create_strategy(goal: str = Form(...), target: str = Form("")):
        """Generate a plan. Slow — a full brief plus a reasoning call — so it
        runs on the request thread here for the same reason the advisor does
        under Vercel: a background thread would be killed with the function."""
        try:
            row = strategy.generate(goal, target=target.strip() or None)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return RedirectResponse(f"/strategy/{row.id}", status_code=303)

    @app.get("/strategy/{strategy_id}", response_class=HTMLResponse)
    def strategy_detail(request: Request, strategy_id: int):
        with get_session() as session:
            row = session.get(Strategy, strategy_id)
            if row is None:
                return HTMLResponse("Strategy not found", status_code=404)
            steps = (
                session.query(StrategyStep)
                .filter(StrategyStep.strategy_id == strategy_id)
                .order_by(StrategyStep.position, StrategyStep.id)
                .all()
            )
            checkpoints = (
                session.query(StrategyCheckpoint)
                .filter(StrategyCheckpoint.strategy_id == strategy_id)
                .order_by(StrategyCheckpoint.date.asc())
                .all()
            )
            done = sum(1 for s in steps if s.status == "done")
            # Union of baseline and every checkpoint's keys, so a metric that
            # only started being collected later still gets a row.
            keys = [k for k in row.baseline if k != "as_of"]
            for cp in checkpoints:
                for k in cp.metrics:
                    if k != "as_of" and k not in keys:
                        keys.append(k)
            for obj in [row, *steps, *checkpoints]:
                session.expunge(obj)
        return render(
            request, "strategy_detail.html",
            nav="strategy",
            strategy=row, steps=steps, checkpoints=checkpoints,
            done=done, metric_keys=keys,
        )

    @app.post("/strategy/{strategy_id}/activate")
    def activate_strategy(strategy_id: int):
        strategy.activate(strategy_id)
        return RedirectResponse(f"/strategy/{strategy_id}", status_code=303)

    @app.post("/strategy/step/{step_id}/run")
    def run_strategy_step(step_id: int):
        try:
            step = strategy.execute(step_id)
        except strategy.StepError as e:
            with get_session() as session:
                row = session.get(StrategyStep, step_id)
                if row is not None:
                    row.result = str(e)[:2000]
                    sid = row.strategy_id
            return RedirectResponse(f"/strategy/{sid}", status_code=303)
        except KeyError:
            return JSONResponse({"error": "unknown step"}, status_code=404)
        return RedirectResponse(f"/strategy/{step.strategy_id}", status_code=303)

    @app.post("/strategy/step/{step_id}/status")
    def set_strategy_step(step_id: int, status: str = Form(...)):
        try:
            strategy.set_step_status(step_id, status)
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        with get_session() as session:
            row = session.get(StrategyStep, step_id)
            sid = row.strategy_id if row else 0
        return RedirectResponse(f"/strategy/{sid}", status_code=303)

    # ── Local SEO ───────────────────────────────────────────────────
    @app.get("/local", response_class=HTMLResponse)
    def local_page(request: Request, window: int = 90):
        window = max(28, min(window, 365))
        return render(
            request, "local.html",
            data=reporting.local_seo(days=window),
            window=window,
            advisor_scope="local",
        )

    # ── Competitors ─────────────────────────────────────────────────
    @app.get("/competitors", response_class=HTMLResponse)
    def competitors_page(request: Request, window: int = 90):
        window = max(28, min(window, 365))
        data = reporting.competitors(days=window)
        return render(
            request, "competitors.html",
            data=data,
            window=window,
            # Posts carry a competitor_id, not the row — one lookup here
            # beats a join per row in the template.
            competitor_names={
                row["competitor"].id: row["competitor"].name
                for row in data["overview"]
            },
            advisor_scope="competitors",
        )

    @app.get("/competitors/product/{product_id}", response_class=HTMLResponse)
    def competitor_product_page(
        request: Request, product_id: int, window: int = 90
    ):
        """One matched product's price over time, theirs against ours."""
        window = max(14, min(window, 365))
        history = reporting.competitor_price_history(product_id, days=window)
        if not history["found"]:
            return HTMLResponse("Competitor product not found", status_code=404)
        return render(
            request, "competitor_product.html",
            nav="competitors",
            history=history,
            window=window,
            chart=charts.price_chart(
                history["ours"],
                history["theirs"],
                our_label="Our price",
                their_label=(
                    history["competitor"].name if history["competitor"] else "Theirs"
                ),
            ),
        )

    @app.post("/competitors/match/{match_id}")
    def decide_match(match_id: int, decision: str = Form(...)):
        """Confirm or reject one proposed product match.

        Rejections are stored, not deleted — see `CompetitorMatch`. A deleted
        rejection is a proposal the next run makes all over again.
        """
        if decision not in (MatchStatus.confirmed.value, MatchStatus.rejected.value):
            return JSONResponse({"error": f"bad decision {decision}"}, status_code=400)
        with get_session() as session:
            match = session.get(CompetitorMatch, match_id)
            if match is None:
                return JSONResponse({"error": "unknown match"}, status_code=404)
            match.status = decision
            match.decided_at = datetime.now(timezone.utc).replace(tzinfo=None)
        return RedirectResponse("/competitors", status_code=303)

    @app.post("/competitors/post/{post_id}/counter")
    def counter_post(post_id: int):
        """Queue an article answering a competitor's post.

        Writes a `CalendarEntry` into the pipeline's own queue rather than
        drafting anything here: the pipeline already knows how to research,
        write, QA and publish, and a second path into Shopify is the last
        thing this needs. This adds a topic and gets out of the way — the
        same thing `blog-pipeline add-topic` does, including the Linear
        issue, so a countered post is reviewed exactly like every other
        queued article rather than arriving through a side door.

        Target keywords come from our own striking-distance terms that appear
        in their title. Countering a competitor is only worth doing on a term
        Google already associates with this site; without that it's just
        writing about whatever they wrote about.
        """
        from blog_pipeline.db.models import CalendarEntry, EntryStatus, TopicSource
        from blog_pipeline.db.session import get_session as pipeline_session
        from blog_pipeline.graphs.calendar_graph import _get_or_create_calendar

        with get_session() as session:
            post = session.get(CompetitorPost, post_id)
            if post is None:
                return JSONResponse({"error": "unknown post"}, status_code=404)
            if post.countered_at:
                # Already queued. Not an error — a double-submitted form.
                return RedirectResponse("/competitors", status_code=303)
            topic = post.title.strip()[:500]
            keywords = reporting.our_terms_in(topic)
            post.countered_at = datetime.now(timezone.utc).replace(tzinfo=None)
            post.countered_topic = topic

        notes = (
            "Countering a competitor post. Write the better answer for a "
            "Langley, BC audience and make the local angle explicit — that is "
            "the part a competitor without a local showroom cannot copy."
        )
        with pipeline_session() as session:
            calendar = _get_or_create_calendar(session)
            entry = CalendarEntry(
                calendar_id=calendar.id,
                scheduled_date=date.today(),
                topic=topic,
                target_keywords=keywords,
                source=TopicSource.manual,
                status=EntryStatus.queued,
                notes=notes,
            )
            session.add(entry)
            _sync_topic_to_linear(entry, topic, keywords, notes)
        return RedirectResponse("/competitors?countered=1", status_code=303)

    # ── Advisor ─────────────────────────────────────────────────────
    @app.post("/advisor/{scope}/generate")
    def generate_advice(scope: str):
        """Generate on demand. Slow (a reasoning model), so off the request
        thread — except on Vercel, where there is no thread to be off to."""
        if scope not in advisor.SCOPES:
            return JSONResponse({"error": f"unknown scope {scope}"}, status_code=404)
        if _advice_running(scope):
            return RedirectResponse(f"/{_scope_path(scope)}", status_code=303)
        if is_serverless():
            # Same reason as "Run now" in run_job_route: the function is
            # frozen the moment this redirect is sent, so a background thread
            # is killed before the model answers and the button does nothing
            # at all. Generate first, then redirect to a page that already
            # has the note on it — no ?generating=1 poll to wait on.
            try:
                advisor.generate(scope)
            except Exception:  # noqa: BLE001 - recorded on the note itself
                log.exception("advisor generation for %s failed", scope)
            return RedirectResponse(f"/{_scope_path(scope)}", status_code=303)
        _start_advice(scope)
        return RedirectResponse(
            f"/{_scope_path(scope)}?generating=1", status_code=303
        )

    @app.get("/advisor/{scope}/status")
    def advice_status(scope: str):
        note = advisor.latest_note(scope) if scope in advisor.SCOPES else None
        return {
            "running": _advice_running(scope),
            "generated_at": _fmt_when(note.created_at) if note else None,
            "error": note.error if note else None,
        }

    @app.post("/advisor/action/{action_id}")
    def resolve_action(
        action_id: int, status: str = Form(...), back: str = Form("/")
    ):
        try:
            advisor.resolve(action_id, status)
        except ValueError:
            pass
        return RedirectResponse(back or "/", status_code=303)

    # ── Experiments ─────────────────────────────────────────────────
    @app.get("/experiments", response_class=HTMLResponse)
    def experiments_page(request: Request, error: str = ""):
        with get_session() as session:
            rows = session.query(Experiment).order_by(Experiment.id.desc()).all()
            counts = {}
            for row in rows:
                session.expunge(row)
                members = session.query(ExperimentProduct).filter(
                    ExperimentProduct.experiment_id == row.id
                ).all()
                counts[row.id] = {
                    "treatment": sum(1 for m in members if m.cohort == "treatment"),
                    "control": sum(1 for m in members if m.cohort == "control"),
                }
        return render(
            request, "experiments.html",
            experiments=rows, counts=counts,
            advisor_scope="experiments",
            variables=experiments.VARIABLES, error=error,
        )

    @app.post("/experiments")
    def create_experiment(
        name: str = Form(...),
        variable: str = Form(...),
        hypothesis: str = Form(""),
        baseline_days: int = Form(28),
    ):
        if variable not in experiments.VARIABLES:
            return RedirectResponse(
                f"/experiments?error={quote('Unknown variable ' + variable)}",
                status_code=303,
            )
        with get_session() as session:
            existing = session.query(Experiment).filter(
                Experiment.name == name.strip()
            ).one_or_none()
            if existing is not None:
                return RedirectResponse(
                    f"/experiments?error={quote('That name is already used.')}",
                    status_code=303,
                )
            row = Experiment(
                name=name.strip(), variable=variable,
                hypothesis=hypothesis.strip() or None,
                baseline_days=max(7, min(int(baseline_days), 180)),
            )
            session.add(row)
            session.flush()
            new_id = row.id
        return RedirectResponse(f"/experiments/{new_id}", status_code=303)

    @app.get("/experiments/{experiment_id}", response_class=HTMLResponse)
    def experiment_page(
        request: Request, experiment_id: int, error: str = "", saved: int = 0
    ):
        try:
            result = experiments.score(experiment_id)
        except ValueError:
            return HTMLResponse("Experiment not found", status_code=404)
        with get_session() as session:
            members = session.query(ExperimentProduct).filter(
                ExperimentProduct.experiment_id == experiment_id
            ).all()
            for m in members:
                session.expunge(m)
            gids = [m.product_gid for m in members]
            products = {
                p.product_gid: p
                for p in session.query(ShopifyProduct)
                .filter(ShopifyProduct.product_gid.in_(gids or [""]))
                .all()
            }
            for p in products.values():
                session.expunge(p)
        return render(
            request, "experiment.html",
            nav="experiments",
            result=result, experiment=result["experiment"],
            members=members, products=products,
            treatment=[m for m in members if m.cohort == "treatment"],
            control=[m for m in members if m.cohort == "control"],
            error=error, saved=bool(saved),
            min_group=experiments.MIN_GROUP,
        )

    @app.post("/experiments/{experiment_id}/members")
    def add_members(
        experiment_id: int,
        cohort: str = Form(...),
        handles: str = Form(""),
    ):
        """Add products to a cohort by handle. Draft experiments only —
        membership is frozen once a baseline exists."""
        wanted = [h.strip() for h in handles.replace(",", "\n").split("\n") if h.strip()]
        with get_session() as session:
            experiment = session.get(Experiment, experiment_id)
            if experiment is None:
                return HTMLResponse("Not found", status_code=404)
            if experiment.status != "draft":
                return RedirectResponse(
                    f"/experiments/{experiment_id}?error="
                    + quote("Membership is frozen once the experiment starts."),
                    status_code=303,
                )
            found = session.query(ShopifyProduct).filter(
                ShopifyProduct.handle.in_(wanted or [""])
            ).all()
            existing = {
                g for (g,) in session.query(ExperimentProduct.product_gid)
                .filter(ExperimentProduct.experiment_id == experiment_id).all()
            }
            for product in found:
                if product.product_gid in existing:
                    continue
                session.add(ExperimentProduct(
                    experiment_id=experiment_id,
                    product_gid=product.product_gid,
                    cohort="treatment" if cohort == "treatment" else "control",
                ))
            missing = sorted(set(wanted) - {p.handle for p in found})
        if missing:
            return RedirectResponse(
                f"/experiments/{experiment_id}?error="
                + quote(f"No product with handle: {', '.join(missing[:5])}"),
                status_code=303,
            )
        return RedirectResponse(f"/experiments/{experiment_id}?saved=1", status_code=303)

    @app.post("/experiments/{experiment_id}/suggest-controls")
    def suggest_controls(experiment_id: int, count: int = Form(50)):
        """Fill the control group with impression-matched candidates."""
        with get_session() as session:
            experiment = session.get(Experiment, experiment_id)
            if experiment is None or experiment.status != "draft":
                return RedirectResponse(
                    f"/experiments/{experiment_id}?error="
                    + quote("Controls can only be added while still a draft."),
                    status_code=303,
                )
            treatment_gids = [
                g for (g,) in session.query(ExperimentProduct.product_gid).filter(
                    ExperimentProduct.experiment_id == experiment_id,
                    ExperimentProduct.cohort == "treatment",
                ).all()
            ]
        if not treatment_gids:
            return RedirectResponse(
                f"/experiments/{experiment_id}?error="
                + quote("Add treatment products first — controls are matched to them."),
                status_code=303,
            )
        proposed = experiments.propose_controls(
            treatment_gids, count=max(1, min(int(count), 200))
        )
        with get_session() as session:
            existing = {
                g for (g,) in session.query(ExperimentProduct.product_gid)
                .filter(ExperimentProduct.experiment_id == experiment_id).all()
            }
            for candidate in proposed:
                gid = candidate["product"].product_gid
                if gid in existing:
                    continue
                session.add(ExperimentProduct(
                    experiment_id=experiment_id, product_gid=gid, cohort="control",
                ))
        return RedirectResponse(f"/experiments/{experiment_id}?saved=1", status_code=303)

    @app.post("/experiments/{experiment_id}/start")
    def start_experiment(experiment_id: int):
        try:
            experiments.freeze_baseline(experiment_id)
        except ValueError as exc:
            return RedirectResponse(
                f"/experiments/{experiment_id}?error={quote(str(exc))}",
                status_code=303,
            )
        return RedirectResponse(f"/experiments/{experiment_id}?saved=1", status_code=303)

    @app.post("/experiments/{experiment_id}/apply")
    async def apply_experiment(request: Request, experiment_id: int):
        """Write the treatment to Shopify. Changes live product SEO."""
        form = await request.form()
        values = {
            key[len("value-"):]: str(value)
            for key, value in form.items()
            if key.startswith("value-") and str(value).strip()
        }
        if not values:
            return RedirectResponse(
                f"/experiments/{experiment_id}?error="
                + quote("Nothing to apply — fill in at least one new value."),
                status_code=303,
            )
        try:
            summary = experiments.apply_treatment(experiment_id, values)
        except (ValueError, ShopifyError) as exc:
            return RedirectResponse(
                f"/experiments/{experiment_id}?error={quote(str(exc)[:400])}",
                status_code=303,
            )
        if summary["failed"]:
            first = next(
                (r["error"] for r in summary["results"] if not r["ok"]), "unknown"
            )
            return RedirectResponse(
                f"/experiments/{experiment_id}?error="
                + quote(f"{summary['applied']} applied, {summary['failed']} failed. "
                        f"First error: {first[:250]}"),
                status_code=303,
            )
        return RedirectResponse(f"/experiments/{experiment_id}?saved=1", status_code=303)

    # ── Alerts ──────────────────────────────────────────────────────
    @app.get("/alerts", response_class=HTMLResponse)
    def alerts_page(request: Request, show: str = "open"):
        alerts.ensure_default_rules()
        with get_session() as session:
            rules = session.query(AlertRule).order_by(AlertRule.id).all()
            for r in rules:
                session.expunge(r)
        return render(
            request, "alerts.html",
            items=alerts.open_alerts() if show == "open" else alerts.recent_alerts(),
            show=show,
            rules=rules,
            kinds={k.key: k for k in alerts.KINDS},
            open_count=len(alerts.open_alerts()),
            slack_ready=pipeline().has_slack,
        )

    @app.post("/alerts/{alert_id}/ack")
    def ack_alert(alert_id: int):
        alerts.acknowledge(alert_id)
        return RedirectResponse("/alerts", status_code=303)

    @app.post("/alerts/rules")
    async def save_alert_rules(request: Request):
        form = await request.form()
        with get_session() as session:
            for rule in session.query(AlertRule).all():
                rule.enabled = f"enabled-{rule.id}" in form
                rule.notify = f"notify-{rule.id}" in form
                raw = form.get(f"threshold-{rule.id}")
                if raw not in (None, ""):
                    try:
                        rule.threshold = float(raw)
                    except (TypeError, ValueError):
                        # Leave the old threshold rather than zeroing it — a
                        # rule silently reset to 0 fires on everything.
                        log.warning("ignored bad threshold %r for rule %s",
                                    raw, rule.id)
        return RedirectResponse("/alerts", status_code=303)

    # ── Google Ads ──────────────────────────────────────────────────
    @app.get("/ads", response_class=HTMLResponse)
    def ads_page(request: Request, window: int = 28):
        window = max(7, min(window, 180))
        return render(
            request, "ads.html",
            ads=reporting.ads_overview(window_days=window),
            advisor_scope="ads",
            window=window,
            last_sync=last_runs().get("ads_windsor"),
            windsor_configured=get_settings().has_windsor,
        )

    # ── Jobs ────────────────────────────────────────────────────────
    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(request: Request):
        last = last_runs()
        nexts = scheduler.next_run_times()
        rows = [
            {
                "spec": spec,
                "last": last.get(spec.name),
                "next": nexts.get(spec.name),
                "running": is_running(spec.name),
                "scheduled": bool(
                    spec.enabled_key and store.get(spec.enabled_key)
                ),
            }
            for spec in all_jobs()
        ]
        return render(
            request,
            "jobs.html",
            jobs=rows,
            history=recent_runs(limit=40),
            scheduler_on=get_settings().enable_scheduler_effective,
        )

    @app.post("/jobs/{name}/run")
    def run_job_route(name: str):
        # Resolve the name here, before anything else. `is_running` will
        # happily mint a lock for a name that was never registered, and the
        # runner's own KeyError would be raised on the background thread —
        # where nobody is listening and the response has already said 200.
        try:
            get_job(name)
        except KeyError:
            return JSONResponse({"error": f"unknown job {name}"}, status_code=404)
        if is_running(name):
            return JSONResponse(
                {"started": False, "job": name, "reason": "already running"},
                status_code=409,
            )
        if is_serverless():
            # A background thread does not survive here: Vercel freezes the
            # function the moment the response is sent, so the thread is
            # killed mid-job and its job_run row stays "running" forever —
            # the button appears to work and silently does nothing. Run it on
            # the request thread instead and report the real outcome, the
            # same way the cron endpoints do.
            #
            # The cost is that a job slower than the function's maxDuration
            # returns a gateway timeout. That's the better failure: loud, and
            # visible on the page that asked for it.
            try:
                run = run_job(name, trigger="manual")
            except JobAlreadyRunning:
                return JSONResponse(
                    {"started": False, "job": name, "reason": "already running"},
                    status_code=409,
                )
            return JSONResponse(
                {
                    "started": True,
                    "job": name,
                    "status": run.status,
                    "rows": run.rows,
                    "error": run.error,
                }
            )
        run_in_background(name, trigger="manual")
        return JSONResponse({"started": True, "job": name})

    @app.get("/jobs/status")
    def jobs_status():
        """Polled by the jobs page so a running sync updates without a reload."""
        last = last_runs()
        return {
            "jobs": {
                spec.name: {
                    "running": is_running(spec.name),
                    "status": last[spec.name].status if spec.name in last else None,
                    "started_at": (
                        _fmt_when(last[spec.name].started_at)
                        if spec.name in last else None
                    ),
                    "rows": last[spec.name].rows if spec.name in last else None,
                    "error": last[spec.name].error if spec.name in last else None,
                }
                for spec in all_jobs()
            }
        }

    # ── Settings ────────────────────────────────────────────────────
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, saved: int = 0, error: str = ""):
        with get_session() as session:
            competitors = (
                session.query(Competitor).order_by(Competitor.name.asc()).all()
            )
            for c in competitors:
                session.expunge(c)
        return render(
            request,
            "settings.html",
            groups=store.groups(),
            values=store.get_all(),
            competitors=competitors,
            saved=bool(saved),
            error=error,
            integrations=_integration_status(),
        )

    @app.post("/settings")
    async def save_settings(request: Request):
        form = await request.form()
        updates: list[tuple[str, object]] = []
        for spec in store.SPECS:
            if spec.kind == "bool":
                # An unchecked box posts nothing at all, which is the only way
                # to distinguish it from unchecked — hence the explicit False.
                updates.append((spec.key, spec.key in form))
            elif spec.key in form:
                updates.append((spec.key, form[spec.key]))
        try:
            store.set_many(updates)
        except (ValueError, TypeError) as exc:
            return RedirectResponse(
                f"/settings?error={str(exc)[:200]}", status_code=303
            )
        # Schedules live in the same store, so a save can change when jobs run.
        scheduler.reschedule()
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/settings/competitors")
    def add_competitor(
        name: str = Form(...),
        base_url: str = Form(...),
        price_selector: str = Form(""),
        notes: str = Form(""),
    ):
        with get_session() as session:
            session.add(
                Competitor(
                    name=name.strip(),
                    base_url=base_url.strip(),
                    price_selector=price_selector.strip() or None,
                    notes=notes.strip() or None,
                )
            )
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/settings/competitors/{competitor_id}/delete")
    def delete_competitor(competitor_id: int):
        with get_session() as session:
            row = session.get(Competitor, competitor_id)
            if row is not None:
                session.delete(row)
        return RedirectResponse("/settings?saved=1", status_code=303)

    return app


def _integration_status() -> list[dict]:
    """Which credentials are actually live, read from the pipeline's settings.

    Shown on the settings page because "the sync returns nothing" and "the key
    was never set" look identical from the outside, and the second one is
    almost always the answer.
    """
    s = pipeline()
    return [
        {
            "name": "Search Console",
            "ok": s.has_search_console,
            "detail": s.gsc_property or "GSC_SITE_URL / GSC_CREDENTIALS_JSON unset",
            "used_by": "Site + page search metrics (Phase 0/1)",
        },
        {
            "name": "Google Analytics 4",
            "ok": s.has_analytics,
            "detail": s.ga4_property_id or "GA4_PROPERTY_ID unset",
            "used_by": "Sessions and call-click conversions (Phase 1)",
        },
        {
            "name": "Shopify Admin API",
            "ok": s.has_shopify,
            "detail": s.shopify_store_domain or "SHOPIFY_STORE_DOMAIN unset",
            "used_by": "Product snapshot, blog management (Phase 1/2)",
        },
        {
            "name": "Linear",
            "ok": s.has_linear,
            "detail": s.linear_team or "LINEAR_TEAM unset",
            "used_by": "Image-suggestion issues from the refresh pass (Phase 2)",
        },
    ]


app = create_app()
