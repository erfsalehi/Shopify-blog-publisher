# Blog Publisher — Topic Research → Linear + Shopify Pipeline

An agentic pipeline that researches blog topics, maintains a rolling content
calendar in Linear, and drafts SEO-optimized articles — then **auto-publishes
confident ones live to Shopify** while routing anything uncertain to Linear
for a human to review and publish by hand. Built on LangGraph, routing all
LLM calls through Google AI Studio's free-tier Gemini API and tracing to
LangSmith.

Implements the full PRD (`PRD Topic Research to Publish Pipeline.md`).

## Architecture

Two graphs plus a CLI that cron drives:

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
  `OPENROUTER_API_KEY` (images — separate provider from `GOOGLE_API_KEY` since
  AI Studio's free tier has no image quota; pay-as-you-go, fractions of a
  cent/image), `DATAFORSEO_*` (SERP data — falls back to LLM-only research),
  `SLACK_WEBHOOK_URL` (digest — logs to stdout instead),
  `LANGSMITH_API_KEY` (tracing). `SEED_KEYWORDS` is also optional — leave it
  unset and `run-calendar` auto-researches seed keywords from `NICHE`.

### Linear setup

1. Linear → Settings → API → create a personal or workspace API key, in the
   workspace you want the calendar to live in (e.g.
   `https://linear.app/<your-workspace>`).
2. Set `LINEAR_API_KEY` and `LINEAR_TEAM` (the team's name or key, e.g.
   `"Content"` or `"CON"`) in `.env`.
3. `LINEAR_PROJECT` (default `Blog Content Calendar`) is auto-created under
   that team on first `run-calendar` if it doesn't already exist, as is a
   `Blog` label. No manual board setup required.
4. Workflow states are matched by name against whatever the team already
   has (`Backlog`, `In Progress`, `Ready to Review`, `Needs Adjustments`,
   `Blocked`, `Done`, ...) — standard Linear defaults work out of the box.
   `LINEAR_PUBLISHED_STATE` (default `Done`) is where an issue lands once its
   article auto-publishes.

### Shopify setup (optional — enables auto-publish)

Leave this out entirely to run Linear-only. To turn on auto-publishing of
confident articles:

1. Shopify admin → **Settings → Apps and sales channels → Develop apps**. If
   the button is disabled, the store owner clicks **"Allow custom app
   development"** first (one-time).
2. **Create an app** → **Configure Admin API scopes** → enable `write_content`
   and `read_products` (add `write_files` only if you enable images).
3. **Install app** → reveal the **Admin API access token** (shown once, starts
   with `shpat_`) → `SHOPIFY_ACCESS_TOKEN`.
4. Set `SHOPIFY_STORE_DOMAIN` to the permanent `*.myshopify.com` domain
   (Settings → Domains), **not** a custom domain. Leave `SHOPIFY_BLOG_ID`
   blank to publish under the store's first blog.
5. `ENABLE_SHOPIFY_PUBLISH=true` (default) turns auto-publish on; set it
   `false` to keep credentials configured but pause publishing.

With Shopify configured, articles that pass QA with confidence ≥
`CONFIDENCE_THRESHOLD` publish **live** immediately. Lower the threshold to
publish more aggressively, raise it to route more to Linear for review first.

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

# Inspect:
blog-pipeline calendar           # upcoming queue (with Linear issue ids)
blog-pipeline status             # health metrics dashboard
```

**Confident articles** (with Shopify configured) publish live automatically —
their Linear issue is moved to `Done` with the live URL, as a record.
**Everything else** lands in Linear for review: open the issue and the full
draft (as Markdown), SEO meta, generated images, and QA notes are in the
description, with a raw-HTML code block at the bottom for pasting into your
CMS. Review, edit, publish, and move the issue to `Done`. Either way, the
pipeline never touches an article again after it reaches its terminal state.

## Scheduling in production

`.github/workflows/` contains weekly (calendar) and daily (draft) crons.
They require `DATABASE_URL` pointed at **Postgres** — the SQLite default does
not persist across ephemeral CI runners. Locally, use the CLI directly or
Windows Task Scheduler / cron.

## Trigger from WhatsApp (off-schedule)

Beyond the crons, you can trigger a run on demand by messaging your WhatsApp
number (Meta Cloud API). Message `draft: <topic>`, `daily`, `calendar`, or
`status` and the result comes back in the chat.

```bash
pip install -e ".[whatsapp]"
blog-pipeline serve          # FastAPI webhook on :8000/webhook
```

Point your Meta app's webhook at `https://<host>/webhook`. Full step-by-step
(Meta app, tokens, signature verification, the 24-hour window) is in
[docs/whatsapp-setup.md](docs/whatsapp-setup.md). Only allow-listed numbers can
trigger it, and every webhook call is signature-verified.

## Testing

```bash
pytest
```

Unit tests cover the deterministic logic (dedup, cadence scheduling, SEO
rubric, Linear payload construction, QA confidence routing) with LLM/HTTP
mocked, so they run offline with no API keys.

## AI SEO (GEO)

Beyond classic on-page SEO, the pipeline optimizes for **AI answer engines**
(ChatGPT, Claude, Gemini, Google AI Overviews, Perplexity) so your content can
be parsed and cited there too (`ENABLE_GEO=true`):

- **Answer-first drafting** — a direct answer up top, question-style headings,
  self-contained factual sentences that quote well out of context.
- **A visible "Key takeaways" box + FAQ section** — the extractable Q&A those
  engines favor.
- **JSON-LD structured data** (`Article` + `FAQPage` schema.org) embedded in the
  article body so crawlers read the page as data, not just prose.

One thing the pipeline can't do per-article: **AI crawler access is a
store-level setting.** For AI engines to ingest your site at all, your Shopify
`robots.txt` must allow their bots (`GPTBot`, `ClaudeBot`, `Google-Extended`,
`PerplexityBot`, `CCBot`). Edit `robots.txt.liquid` in your theme once to allow
them.

## Known simplifications

- **Internal linking** pulls anchors from the store's own Shopify catalog
  (collections + service/local pages, not individual product SKUs) and links
  the first matching phrase in the body; the SEO rubric scores it (5 pts). It
  only fires when an article's prose actually contains a category/page name, so
  some articles get several links and others none. With Shopify unconfigured
  it's skipped.
- **Images** are generated via OpenRouter (Gemini image models) and hosted on
  Linear's own file storage (`fileUpload` with `makePublic: true`) — no
  fal.ai account or separate image host needed. Re-host them elsewhere when
  you actually publish if you need a URL independent of Linear.
- **Semantic dedup** uses local `fastembed` ONNX embeddings (no extra API
  key); the model downloads on first `run-calendar`.
