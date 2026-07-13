# Blog Publisher — Topic Research → Linear + Shopify Pipeline

An agentic pipeline that researches blog topics, maintains a rolling content
calendar in Linear, and drafts SEO-optimized articles — then **auto-publishes
confident ones live to Shopify** while routing anything uncertain to Linear
for a human to review and publish by hand. Built on LangGraph, routing all
LLM calls through Google AI Studio's free-tier Gemini API and tracing to
LangSmith.

Implements the full PRD (`PRD Topic Research to Publish Pipeline.md`).

**Docs index:** [Linear setup](#linear-setup) · [Shopify setup](#shopify-setup-optional--enables-auto-publish)
· [AI SEO / GEO](#ai-seo-geo) · [WhatsApp trigger](docs/whatsapp-setup.md)
· [Railway deploy](docs/railway-deploy.md) · [robots.txt for AI crawlers](docs/robots.txt.liquid)
· [llms.txt](docs/llms.txt)

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
  `OPENROUTER_API_KEY` (image *generation* — off by default; see
  [Images](#images) below), `DATAFORSEO_*` (SERP data — falls back to
  LLM-only research), `SLACK_WEBHOOK_URL` (per-article + digest notifications
  — logs to stdout instead), `WHATSAPP_*` (trigger runs on demand — see
  [docs/whatsapp-setup.md](docs/whatsapp-setup.md)), `LANGSMITH_API_KEY`
  (tracing). `SEED_KEYWORDS` is also optional — leave it unset and
  `run-calendar` auto-researches seed keywords from `NICHE`.
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
  copy-paste-into-Shopify step for articles that already cleared QA.

Lower `CONFIDENCE_THRESHOLD` to publish more aggressively, raise it to route
more to Linear for review first.

### Store promotion

With `SHOP_PROMO=true` (default, requires `BUSINESS_NAME`), the draft agent is
told the publishing business *sells* what the article discusses, so it weaves
in a soft, genuine mention or two — never a hard sell — and every article gets
a closing "Shop with us" call-to-action linking to `PUBLIC_DOMAIN`. Set
`SHOP_PROMO=false` to turn this off and keep articles purely editorial.

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

Don't have a Postgres instance handy? [docs/railway-deploy.md](docs/railway-deploy.md)
covers provisioning one on Railway (needed anyway if you deploy the WhatsApp
webhook below) and pointing both the crons and the webhook at it, via a
Railway **TCP Proxy** connection string for the GitHub Actions secret.

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
  key); the model downloads on first `run-calendar`.
- **`llms.txt`** is generated but not auto-hosted — Shopify has no built-in
  route for it (unlike `robots.txt.liquid`, which Shopify does support
  natively). See the note above.
