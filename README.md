# Blog Publisher — Topic Research → Linear + Shopify Pipeline

An agentic pipeline that researches blog topics, maintains a rolling content
calendar in Linear, and drafts SEO-optimized articles — then **auto-publishes
confident ones to Shopify** while routing anything uncertain to Linear for a
human to review. It also **measures what actually happened** (Google Search
Console + GA4) and **rewrites its own decaying posts** on that evidence. Built
on LangGraph, routing all LLM calls through Google AI Studio's free-tier
Gemini API and tracing to LangSmith.

Implements the full PRD (`PRD Topic Research to Publish Pipeline.md`).

**Docs index:** [Linear setup](#linear-setup) · [Shopify setup](#shopify-setup-optional--enables-auto-publish)
· [Performance data](docs/search-console.md) · [Refreshing old posts](#refreshing-old-posts)
· [AI SEO / GEO](#ai-seo-geo) · [WhatsApp trigger](docs/whatsapp-setup.md)
· [Railway deploy](docs/railway-deploy.md) · [robots.txt for AI crawlers](docs/robots.txt.liquid)
· [llms.txt](docs/llms.txt)

## Architecture

Two graphs, a refresh pass, and a CLI that cron drives:

- **Calendar graph** (weekly): check coverage → topic research → dedupe
  (exact + semantic) → assign publish dates per cadence → persist → **create
  a Linear issue per new topic** (`Backlog`, due date = scheduled date) →
  Slack digest.
- **Article graph** (per due entry): outline → draft → SEO → images → QA →
  **route on QA outcome**:
  - **Confident pass** (verdict `pass`, confidence ≥ `CONFIDENCE_THRESHOLD`)
    *and Shopify configured* → **publish live to Shopify**, then move the
    Linear issue to the published state (`Done`) with the live URL recorded.
  - **Anything else** (low confidence, `review`, `block`, or Shopify not
    configured) → **sync to Linear only** (`Ready to Review` /
    `Needs Adjustments` / `Blocked`) for a human to review and publish by hand.

  A publish failure is non-fatal — the article still syncs to Linear as
  `Needs Adjustments` with the error noted, so no work is lost. Leave Shopify
  unconfigured to run fully Linear-only (nothing auto-publishes).

- **Refresh pass** (weekly, `run-refresh`): pick the live posts that are
  actually losing traffic → rewrite each with its own agent → write back to
  Shopify **in place** → record what changed in Linear. See
  [Refreshing old posts](#refreshing-old-posts). This is the only path that
  edits public content without a human in the loop, so it's the most heavily
  guarded — snapshot before write, refuse-on-asset-loss, dry-run by default.

The store's **existing** posts matter to all three: `import-existing` pulls
every live Shopify article into the database, which is what lets dedup see
articles that predate the pipeline (otherwise research happily re-proposes
topics you published years ago), and gives the refresh pass something to
select from. It's idempotent and runs at the head of the weekly cron.

Model routing (via Google AI Studio's OpenAI-compatible endpoint):

| Stage | Model | Why this one |
|---|---|---|
| Calendar / research | `gemini-3.1-flash-lite-preview` | Weekly-ish volume, generous quota |
| Outline | `gemini-3.1-flash-lite` | Every article; 500 req/day headroom for a structural task |
| Draft | `gemini-2.5-flash` | Reader-visible quality, and verified reliably available (see note below) |
| SEO meta polish | `gemini-3.1-flash-lite` | Every article; lightweight task, shares the generous-quota model |
| QA / fact-check | `gemini-3-flash-preview` | Separate quota bucket from Draft so they don't compete for the same 20/day cap |
| Images | `google/gemini-3.1-flash-lite-image` (via OpenRouter) | Cheap (fractions of a cent/image), hosted on Linear's own storage — see below |

Each stage is pinned to a **different** Gemini model on purpose: AI Studio's
free tier rate-limits per model (as low as 20 requests/day for the non-Lite
Flash models), so spreading stages across models multiplies the effective
daily budget instead of every stage fighting over one model's cap. Override
any of them via `MODEL_CALENDAR` / `MODEL_RESEARCH` / `MODEL_OUTLINE` /
`MODEL_DRAFT` / `MODEL_SEO` / `MODEL_QA` in `.env`.

**Note on model names:** verify against your key's actual catalog before
trusting any model string — `gemini-3-flash` doesn't exist (the real id has a
`-preview` suffix) and `gemini-3.5-flash`/`gemini-flash-latest` were both
returning `503 UNAVAILABLE` (over capacity) as of 2026-07, which is why Draft
defaults to `gemini-2.5-flash` instead. List what's actually live for your
key with:
```bash
curl "https://generativelanguage.googleapis.com/v1beta/models?key=$GOOGLE_API_KEY"
```
Retry `gemini-3.5-flash` for Draft later if you want the newest model once
demand settles.

If you outgrow the free tier, a paid Gemini API key (or Vertex AI) drops in
with no code changes —
just raise `GOOGLE_API_KEY`'s quota and, optionally, fill in `MODEL_RATES` in
`llm.py` for real cost tracking.

## Setup

```bash
python -m venv .venv
. .venv/Scripts/activate      # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
cp .env.example .env          # then fill in keys
blog-pipeline init-db
blog-pipeline config-check    # shows which integrations are live
```

### Required vs optional keys

- **Required:** `GOOGLE_API_KEY` (all LLM stages — free key from
  [aistudio.google.com/apikey](https://aistudio.google.com/apikey)).
  `LINEAR_API_KEY` + `LINEAR_TEAM` to sync the calendar/drafts (without it,
  everything still runs and persists locally, but nothing shows up in Linear).
- **Optional (stage degrades gracefully if unset):** `SHOPIFY_*` (auto-publish
  target — without it the pipeline is Linear-only, nothing auto-publishes),
  `OPENROUTER_API_KEY` (image *generation* — off by default; see
  [Images](#images) below), `DATAFORSEO_*` (SERP data — falls back to
  LLM-only research), `SLACK_WEBHOOK_URL` (per-article + digest notifications
  — logs to stdout instead), `WHATSAPP_*` (trigger runs on demand — see
  [docs/whatsapp-setup.md](docs/whatsapp-setup.md)), `LANGSMITH_API_KEY`
  (tracing), `GSC_CREDENTIALS_JSON` + `GSC_SITE_URL` (real Search Console
  performance — research falls back to market data and refresh to age-ranking
  without it), `GA4_PROPERTY_ID` (AI-referral tracking; credentials fall back
  to the Search Console key) — both in
  [docs/search-console.md](docs/search-console.md). `SEED_KEYWORDS` is also
  optional — leave it unset and `run-calendar` auto-researches seed keywords
  from `NICHE`.
- **Recommended for a real business:** `BUSINESS_NAME` + `BUSINESS_DESCRIPTION`
  (so QA treats your own brand mentions as first-party, not a red flag, and
  drafting writes in your voice), `BUSINESS_LOCATION` (folds a service area
  into local-SEO research when `LOCAL_SEO=true`, the default), `PUBLIC_DOMAIN`
  (in-article links point at your real storefront domain instead of
  `*.myshopify.com`).

### Linear setup

1. Linear → Settings → API → create a personal or workspace API key, in the
   workspace you want the calendar to live in (e.g.
   `https://linear.app/<your-workspace>`).
2. Set `LINEAR_API_KEY` and `LINEAR_TEAM` (the team's name or key, e.g.
   `"Content"` or `"CON"`) in `.env`.
3. `LINEAR_PROJECT` (default `Blog Content Calendar`) is auto-created under
   that team on first `run-calendar` if it doesn't already exist, as is a
   `Blog` label. No manual board setup required.
4. Workflow states are **configurable, not assumed** — `LINEAR_PUBLISHED_STATE`
   / `LINEAR_REVIEW_STATE` / `LINEAR_NEEDS_WORK_STATE` / `LINEAR_BLOCKED_STATE`
   (defaults: `Done` / `Todo` / `Todo` / `Todo`, i.e. a bare-bones team with
   just the stock Linear states). If your team has richer states, point these
   at e.g. `"Ready to Review"` / `"Needs Adjustments"` / `"Blocked"`. A name
   that doesn't exist on your team falls back by state type (started/
   completed/canceled/...) so an issue is never silently left stuck in
   `Backlog` — check your team's actual states first if issues aren't landing
   where you expect (`list_issue_statuses`-style check via the Linear API, or
   just look at Team Settings → Workflow in the Linear UI).

### Shopify setup (optional — enables auto-publish)

Leave this out entirely to run Linear-only. To turn on auto-publishing of
confident articles:

1. Shopify admin → **Settings → Apps and sales channels → Develop apps**. If
   the button is disabled, the store owner clicks **"Allow custom app
   development"** first (one-time).
2. **Create an app** → **Configure Admin API scopes** → enable `write_content`
   and `read_products` (add `write_files` only if you enable image generation).
3. **Install app** → reveal the **Admin API access token** (shown once, starts
   with `shpat_`) → `SHOPIFY_ACCESS_TOKEN`.
4. Set `SHOPIFY_STORE_DOMAIN` to the permanent `*.myshopify.com` domain
   (Settings → Domains), **not** a custom domain. Leave `SHOPIFY_BLOG_ID`
   blank to publish under the store's first blog. Article SEO title/description
   are written via the `global.title_tag`/`global.description_tag` metafields
   (there's no `seo` field on Shopify's `ArticleCreateInput`).
5. Set `PUBLIC_DOMAIN` to your real storefront domain (e.g. `yourstore.com`) —
   internal links and the shop CTA (see [Store promotion](#store-promotion)
   below) use this instead of the `*.myshopify.com` URL.
6. `ENABLE_SHOPIFY_PUBLISH=true` (default) turns auto-publish on; set it
   `false` to keep credentials configured but pause publishing entirely.

With Shopify configured, articles that pass QA with confidence ≥
`CONFIDENCE_THRESHOLD` publish immediately. **How "immediately" behaves is
controlled by `SHOPIFY_PUBLISH_LIVE`:**

- `SHOPIFY_PUBLISH_LIVE=true` (default) — publishes **live**, publicly visible
  right away.
- `SHOPIFY_PUBLISH_LIVE=false` — creates the **same real article in Shopify**
  but **unpublished** (a hidden draft you review in Shopify admin and click
  Publish on yourself). Good default while you're still validating output
  quality: nothing goes public without a final human look, but you skip the
  copy-paste-into-Shopify step for articles that already cleared QA. Also the
  right setting whenever `ENABLE_IMAGES=false`, since drafts then carry literal
  `[IMAGE - ...]` placeholder text that must not reach the public.

Lower `CONFIDENCE_THRESHOLD` to publish more aggressively, raise it to route
more to Linear for review first.

> **`SHOPIFY_PUBLISH_LIVE` governs new articles only — not refreshes.**
> Shopify has no draft revision for an already-published post, so `run-refresh`
> has nothing to stage: editing a live article changes what the public sees, at
> once. There's no flag to change that, which is why the refresh path is
> guarded differently — see [Refreshing old posts](#refreshing-old-posts).
> The asymmetry is usually what you want: a refresh only inherits images that
> are already on the page, while a new draft needs a human to place them.

### Store promotion

With `SHOP_PROMO=true` (default, requires `BUSINESS_NAME`), the draft agent is
told the publishing business *sells* what the article discusses, so it weaves
in a soft, genuine mention or two — never a hard sell — and every article gets
a closing "Shop with us" call-to-action linking to `PUBLIC_DOMAIN`. Set
`SHOP_PROMO=false` to turn this off and keep articles purely editorial.

## Performance data (Search Console + GA4)

Optional, and the only source here that describes **your** site rather than the
market — DataForSEO says what people search; this says what you get shown for,
from what position, and whether that's rising or falling. Full setup (service
account, the grant everyone forgets, the property-id trap) is in
[docs/search-console.md](docs/search-console.md).

```bash
pip install -e ".[gsc]"
blog-pipeline sync-performance --list-sites   # diagnose access
blog-pipeline sync-performance                # Search Console -> DB
blog-pipeline sync-analytics                  # GA4 AI referrals -> DB
blog-pipeline report                          # read it back
```

Two things consume it:

- **Striking-distance queries** feed topic research — terms you already earn
  impressions for from positions 8–30. Google already considers you relevant to
  those, so they're a far shorter path to page one than a high-volume term with
  no history. Weighted heavily in the research prompt.
- **Decay ranking** drives `run-refresh` (below), replacing "oldest first".

`sync-performance` pulls the current window **and the preceding one** in a
single run — decay needs two windows to mean anything, and syncing one window
weekly would leave consecutive snapshots overlapping ~92%, whose difference is
noise. It keeps the four most recent snapshots and prunes the rest (one sync of
a mid-size site is ~30k rows; unpruned that's ~240MB/year).

**AI citation tracking** (`sync-analytics`) is the one place a click from an AI
assistant is directly observable — ChatGPT tags outbound links
`utm_source=chatgpt.com`, and Perplexity/Claude/Copilot arrive as ordinary
referrers. Search Console cannot see any of this, and can't isolate Google's
own AI Overviews either. Two deliberate limits worth knowing:

- **It counts clicks.** Being cited to someone who never clicks is real value
  and invisible here.
- **`google.com`/`bing.com` are excluded** from `AI_SOURCES` — both serve
  ordinary search and AI answers under one referrer, so counting them would
  quietly credit AI for organic traffic. Under-reporting beats a flattering
  number you can't defend. `ai_rows: 0` is a legitimate finding, not an error.

Everything degrades gracefully: unset, research falls back to market data and
refresh falls back to age.

## Refreshing old posts

```bash
blog-pipeline run-refresh                      # dry run — reports, writes nothing
blog-pipeline run-refresh --limit 1 --apply    # rewrites ONE live post
blog-pipeline rollback-refresh --article-id 42 # restore the previous body
```

Picks the live posts **losing the most traffic**, rewrites each, and writes it
back in place. Distinct from the SEO revise loop, which can't do this job: that
one is driven by the rubric and no-ops on a four-year-old article that scores
fine — right keyword density, wrong decade. Staleness isn't a rubric failure.

Candidates are ranked by **absolute impressions lost**, not percentage.
Percentage flatters trivia: a post falling 25→1 is a 96% collapse worth 24
impressions, while 18,272→5,497 is "only" −70% and worth 12,775. Falls back to
oldest-first when there's no Search Console data.

Refreshed posts get the same GEO treatment new ones do (takeaways, pull-quote,
FAQ, JSON-LD) — old articles predate that work, so they're exactly the ones
missing it, and being already-ranking, the ones most worth having cited.

**This is the only path that edits public content unattended**, so:

- `dry_run` is the default everywhere up the stack; applying is opt-in.
- The previous body is snapshotted to `ArticleRevision` **before** the write,
  in its own committed transaction — the only undo Shopify offers for a
  published post.
- A refresh that **drops an image or a link is refused**, not published. The
  prompt asks the model to preserve them; this makes it true. A dropped figure
  would be a broken public page nobody notices.
- Every run opens a Linear issue recording what changed.

## Usage

```bash
# One-off article from a manual topic (safe dry run — nothing synced/published):
blog-pipeline run-article --topic "How to choose running shoes" --dry-run

# Draft it for real (auto-publishes to Shopify if confident + configured,
# otherwise syncs to Linear for review):
blog-pipeline run-article --topic "How to choose running shoes" -k "running shoes,trail shoes"

# Weekly: refresh the topic queue to the coverage target (creates Linear issues).
# --seeds is optional — omit it and seed keywords are auto-researched from --niche:
blog-pipeline run-calendar --niche "outdoor gear"
# ...or supply your own if you already know what you want to target:
blog-pipeline run-calendar --niche "outdoor gear" --seeds "hiking boots,trail running"

# Daily: draft everything scheduled for today and sync each to Linear:
blog-pipeline run-daily

# Pull the store's existing posts in, so dedup can see them (idempotent):
blog-pipeline import-existing

# Weekly: rewrite the worst-decaying live post (dry run unless --apply):
blog-pipeline run-refresh --limit 1

# Inspect:
blog-pipeline calendar           # upcoming queue (with Linear issue ids)
blog-pipeline status             # health metrics dashboard
blog-pipeline report             # what's actually working: decay, striking
                                 # distance, AI referrals, pipeline health
```

**Confident articles** (with Shopify configured) publish live automatically —
their Linear issue is moved to `Done` with the live URL, as a record.
**Everything else** lands in Linear for review: open the issue and the full
draft (as Markdown), SEO meta, generated images, and QA notes are in the
description, with a raw-HTML code block at the bottom for pasting into your
CMS. Review, edit, publish, and move the issue to `Done`. Either way, the
pipeline never touches an article again after it reaches its terminal state.

## Scheduling in production

`.github/workflows/` holds three crons. A worked example — 2 new posts a week
plus 1 refresh, with `CADENCE="2x/week: Tue/Thu"`:

| When (UTC) | Workflow | Does |
|---|---|---|
| Mon 06:00 | `weekly-calendar.yml` | import → sync performance → top up the topic queue |
| Tue 14:00 | `daily-publish.yml` | drafts the entry due today → Shopify |
| Wed 15:00 | `refresh.yml` | rewrites 1 decaying post, **live** |
| Thu 14:00 | `daily-publish.yml` | drafts the entry due today → Shopify |

`daily-publish` runs every day and no-ops when nothing is due, so the cadence —
not the cron — decides how much you publish. `refresh.yml` also accepts a
manual dispatch, which **defaults to a dry run**: the cron is doing the job, a
person clicking Run is usually looking first.

Config lives in GitHub **Variables** (non-secret: `CADENCE`, `LINEAR_TEAM`,
`NICHE`, `BUSINESS_*`, `GA4_PROPERTY_ID`, …) and **Secrets** (`DATABASE_URL`,
`GOOGLE_API_KEY`, `GSC_CREDENTIALS_JSON`, …). Getting that split wrong is
silent — a `${{ vars.X }}` reading a secret just yields `""` forever — so
`tests/test_workflow_env.py` checks every key against `Settings` and every
reference against its own name.

Install extras matter per workflow: `[postgres]` everywhere (psycopg), plus
`[gsc]` wherever `sync-performance`/`sync-analytics` run. A workflow calling
those without the extra dies on an ImportError, so that's also asserted in
`tests/test_workflow_env.py` rather than discovered in a failed run.

All three need `DATABASE_URL` pointed at **Postgres** — the SQLite default does
not survive an ephemeral CI runner. Any managed Postgres works; the crons only
need a publicly reachable connection string:

- **Supabase / Neon** — free tier, no card, ~5 minutes. On Supabase use the
  **Session pooler** string (port 5432): the direct connection is IPv6-only
  unless you pay for the IPv4 add-on, and GitHub's runners have no IPv6 route.
- **Railway** — see [docs/railway-deploy.md](docs/railway-deploy.md). Worth it
  if you're also deploying the WhatsApp webhook (which needs a host anyway);
  otherwise it's a paid app service for a database you can get free. Needs the
  **TCP Proxy** connection string, not the internal one.

`normalize_database_url()` rewrites a bare `postgres://`/`postgresql://` to the
psycopg driver, so any provider's string pastes in unedited.

Locally, use the CLI directly or Windows Task Scheduler / cron.

## Trigger from WhatsApp (off-schedule)

Beyond the crons, you can trigger a run on demand by messaging your WhatsApp
number (Meta Cloud API). Message `draft: <topic>`, `daily`, `calendar`, or
`status` and the result comes back in the chat.

```bash
pip install -e ".[whatsapp]"
blog-pipeline serve          # FastAPI webhook on :8000/webhook
```

That's local dev — Meta needs an HTTPS URL, so tunnel it (`cloudflared` /
`ngrok`) or deploy it properly. Full step-by-step for the Meta side (app
creation, tokens, the webhook verify handshake, signature verification, the
24-hour reply window) is in [docs/whatsapp-setup.md](docs/whatsapp-setup.md).
For an always-on deployment, [docs/railway-deploy.md](docs/railway-deploy.md)
covers running the same `Dockerfile` on Railway (which also gives you the
Postgres instance the crons need). Only allow-listed numbers
(`WHATSAPP_ALLOWED_NUMBERS`) can trigger it, and every webhook call is
signature-verified against `WHATSAPP_APP_SECRET`.

## Testing

```bash
pytest
```

Unit tests cover the deterministic logic (dedup, cadence scheduling, SEO
rubric, Linear payload construction, QA confidence routing, Search Console /
GA4 joins, refresh safety) with LLM/HTTP mocked, so they run offline with no
API keys.

Three areas are tested structurally, because a green suite is not evidence for
them — nothing in the code imports a workflow file, and SQLite doesn't enforce
what Postgres does:

- **Workflow drift** (`test_workflow_env.py`) — every env key exists on
  `Settings`, every `${{ vars.X }}` matches its key, every workflow installs
  the extras its commands need, and optional enrichment can never abort a job.
- **Enum labels** (`test_enum_sync.py`) — `create_all()` builds a Postgres enum
  once and never alters it, so a value added later is missing forever and every
  insert using it fails. SQLite stores enums as text and enforces nothing, so
  the bug is invisible locally by construction. `init-db` reconciles labels.
- **The dedup threshold** (`test_dedup_threshold.py`) — measured against the
  real corpus rather than asserted as a constant. See below.

## AI SEO (GEO)

Beyond classic on-page SEO, the pipeline optimizes for **AI answer engines**
(ChatGPT, Claude, Gemini, Google AI Overviews, Perplexity) so your content can
be parsed and cited there too (`ENABLE_GEO=true`). This implements the
findings of [Aggarwal et al., "GEO: Generative Engine Optimization"
(KDD 2024)](https://arxiv.org/abs/2311.09735) — the study that measured what
actually increases AI citation rates — not just general best practice:

- **Chunking** — AI retrieval (RAG) splits a page into independent ~150-400
  word chunks and retrieves whichever one best answers the query. So the
  outline and draft agents scope every `<h2>` section to that band,
  self-contained (no "as mentioned above"), opening with a 1-2 sentence
  direct-answer "capsule summary" before elaborating. The SEO rubric scores
  the fraction of sections that land in-band (`chunk_compliant_sections`),
  so the auto-revise loop actively fixes sections that don't.
- **Statistics** (study: **+32%** citation rate) — real, defensible,
  well-established numbers stated specifically rather than vaguely. Never
  fabricated — an invented statistic is explicitly barred.
- **A quotable pull-quote** (study: **+41%**, the single biggest lever) — one
  short, honest quote per article, rendered as a `<blockquote>`. Always
  either first-party (the business's own expertise — "Our installers
  find...") or a real, well-known standards body's guidance named plainly.
  Never a fabricated named individual or invented study.
- **Named sources** (study: **+30%**) — real industry standards
  bodies/certifications the article actually cites (e.g. National Wood
  Flooring Association, ANSI), listed visibly and echoed into JSON-LD
  `citation`. Only organizations the model is confident are real.
- **Answer-first drafting** — a direct answer up top, question-style headings,
  self-contained factual sentences that quote well out of context.
- **A visible "Key takeaways" box + FAQ section** — the extractable Q&A those
  engines favor.
- **JSON-LD structured data** (`Article` + `FAQPage` schema.org) embedded in the
  article body so crawlers read the page as data, not just prose.

QA is told these are expected content, not unverifiable claims — it only
flags a quote/source if it looks fabricated (a specific named individual, an
invented organization), never a generic first-party or real-standards-body one.

One thing the pipeline can't do per-article: **AI crawler access is a
store-level setting.** For AI engines to ingest your site at all, your Shopify
`robots.txt` must allow their bots (`GPTBot`, `ClaudeBot`, `Google-Extended`,
`PerplexityBot`, `CCBot`). Paste [docs/robots.txt.liquid](docs/robots.txt.liquid)
into your theme's `templates/robots.txt.liquid` once (Online Store → Themes →
Edit code) to allow them — it starts from Shopify's own default rules so
nothing else changes. [docs/llms.txt](docs/llms.txt) is a companion
`llms.txt`-format summary of the site for AI crawlers/agents, generated from
the store's real collections and pages; Shopify has no native route for
`/llms.txt`, so hosting it needs a small SEO app or a page fallback (see the
file's own notes).

### Images

Off by default (`ENABLE_IMAGES=false`). Two modes:

- **Placeholder mode** (default, `IMAGE_PLACEHOLDERS=true`): each image slot's
  prompt is dropped into the article body as a bold
  `[IMAGE - role: prompt (alt: ...)]` marker. Generate the real image yourself
  (e.g. with Shopify's built-in AI image tool) and swap the marker out before
  publishing — useful if you'd rather not run a second image-gen API. The
  Linear issue also lists every prompt under "Image prompts."
- **Auto-generate** (`ENABLE_IMAGES=true` + `OPENROUTER_API_KEY`): generates via
  OpenRouter (Gemini image models — separate provider from `GOOGLE_API_KEY`,
  since AI Studio's free tier has no image quota; pay-as-you-go, fractions of
  a cent/image) and hosts the result on Linear's own file storage
  (`fileUpload` with `makePublic: true`) — no fal.ai account or separate image
  host needed. Re-host elsewhere if you need a URL independent of Linear.

## Known simplifications

- **Internal linking** pulls anchors from the store's own Shopify catalog
  (collections + service/local pages, not individual product SKUs) and links
  the first matching phrase in the body; the SEO rubric scores it (5 pts). It
  only fires when an article's prose actually contains a category/page name, so
  some articles get several links and others none. With Shopify unconfigured
  it's skipped.
- **Semantic dedup** uses local `fastembed` ONNX embeddings (no extra API
  key); the model downloads on first `run-calendar`. `SEMANTIC_THRESHOLD` is
  **corpus-specific and must be measured, not guessed** — bge-small's cosine
  range is compressed, and inside a single niche everything looks related
  (genuinely unrelated flooring topics still score ~0.71). On the reference
  corpus, new topics peak at 0.815 and real duplicates start at 0.870; the
  threshold sits at 0.855, in the gap. A value chosen by intuition lands inside
  the new-topic band and silently kills good articles — which is exactly what
  0.82 did. Re-measure if the model or the niche changes.
- **`llms.txt`** is generated but not auto-hosted — Shopify has no built-in
  route for it (unlike `robots.txt.liquid`, which Shopify does support
  natively). See the note above.
