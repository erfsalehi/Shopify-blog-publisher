# Shopify Blog Publisher — Topic Research → Publish Pipeline

An agentic pipeline that researches blog topics, maintains a rolling content
calendar, drafts SEO-optimized articles, generates images, QA-checks them, and
publishes to Shopify — with an optional human approval gate. Built on
LangGraph, routing all LLM calls through OpenRouter and tracing to LangSmith.

Implements the full PRD (`PRD Topic Research to Publish Pipeline.md`).

## Architecture

Two graphs plus a CLI that cron drives:

- **Calendar graph** (weekly): check coverage → topic research → dedupe
  (exact + semantic) → assign publish dates per cadence → persist → Slack digest.
- **Article graph** (per due entry): outline → draft → SEO → images → QA →
  *(optional)* human gate → Shopify publish. Checkpointed to SQLite so a run
  paused at the gate can resume days later.

Model routing (via OpenRouter, per PRD §12):

| Stage | Model |
|---|---|
| Calendar / research / outline / SEO | `anthropic/claude-haiku-4.5` |
| Draft | `anthropic/claude-sonnet-5` |
| QA / fact-check | `anthropic/claude-opus-4.8` |
| Images | Flux Schnell (fal.ai) |

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

- **Required:** `OPENROUTER_API_KEY` (all LLM stages). `SHOPIFY_*` to publish.
- **Optional (stage degrades gracefully if unset):** `FAL_KEY` (images),
  `DATAFORSEO_*` (SERP data — falls back to LLM-only research),
  `SLACK_WEBHOOK_URL` (digest/approval — logs to stdout instead),
  `LANGSMITH_API_KEY` (tracing).

### Shopify custom app

1. Shopify admin → Settings → Apps and sales channels → Develop apps → Create.
2. Configure Admin API scopes: `write_content`, `write_files`, `read_products`.
3. Install the app, copy the Admin API access token → `SHOPIFY_ACCESS_TOKEN`.
4. Set `SHOPIFY_STORE_DOMAIN` (e.g. `your-store.myshopify.com`). Leave
   `SHOPIFY_BLOG_ID` blank to auto-use the store's first blog.

## Usage

```bash
# One-off article from a manual topic (safe dry run — nothing published):
blog-pipeline run-article --topic "How to choose running shoes" --dry-run

# Publish it for real:
blog-pipeline run-article --topic "How to choose running shoes" -k "running shoes,trail shoes"

# Weekly: refresh the topic queue to the coverage target:
blog-pipeline run-calendar --niche "outdoor gear" --seeds "hiking boots,trail running"

# Daily: draft+publish everything scheduled for today:
blog-pipeline run-daily

# Human gate (when GATE_MODE=gated or QA flags low confidence):
blog-pipeline pending
blog-pipeline approve 3          # or: blog-pipeline reject 3

# Inspect:
blog-pipeline calendar           # upcoming queue
blog-pipeline status             # health metrics dashboard
```

### Gate modes

- `GATE_MODE=gated` (default): every article pauses for `approve`/`reject`.
- `GATE_MODE=auto`: high-confidence QA passes publish automatically; only
  low-confidence or QA-flagged articles pause. QA `block` verdicts never publish.

## Scheduling in production

`.github/workflows/` contains weekly (calendar) and daily (publish) crons.
They require `DATABASE_URL` pointed at **Postgres** — the SQLite default does
not persist across ephemeral CI runners. Locally, use the CLI directly or
Windows Task Scheduler / cron. Switching stores is just a new `DATABASE_URL`
and Shopify token; no code change.

## Testing

```bash
pytest
```

Unit tests cover the deterministic logic (dedup, cadence scheduling, SEO
rubric, Shopify payload construction, QA confidence routing) with LLM/HTTP
mocked, so they run offline with no API keys.

## Known simplifications

- **Plagiarism check** is against the store's own published titles (no external
  Copyscape-style API); a config hook is left for adding one.
- **Slack approval** links to a preview and shows the CLI command; interactive
  buttons need a hosted endpoint (out of scope until there's a deploy target).
- **Semantic dedup** uses local `fastembed` ONNX embeddings (no extra API key);
  the model downloads on first `run-calendar`.
