# PRD: Topic Research → Linear Handoff Pipeline (Blog Content Automation)

**Owner:** Erfan Salehi
**Status:** Final — Ready to Build
**Last updated:** July 2026

---

## 1. Problem Statement

Manual blog content production is slow and inconsistent: topic selection is ad hoc, SEO research is manual, and drafting takes hours. This creates a bottleneck for stores that need consistent content velocity (e.g., 3–5 posts/week) to compound SEO gains. Publishing itself stays a deliberate human act — the goal is to eliminate everything upstream of it, not to auto-publish unreviewed content to a live storefront.

## 2. Goal

Build an agentic pipeline that autonomously researches topics, drafts SEO-optimized articles, and generates supporting images. The content calendar lives in Linear from the moment a topic is queued. After QA, the pipeline splits by confidence: articles that pass QA with high confidence **auto-publish live to Shopify**, while anything uncertain is routed to Linear for a human to review and publish by hand. This keeps velocity high on safe content without letting low-confidence output reach the storefront unreviewed. Shopify is optional — with it unconfigured the pipeline is fully Linear-only (everything waits for a human).

## 3. Success Metrics

| Metric | Target |
|---|---|
| End-to-end time per article (trigger → published or synced) | < 15 min |
| Human intervention on a confident auto-published article | 0 |
| Human intervention on a low-confidence article | 1 review + manual publish in Linear |
| SEO score (on-page, via internal rubric) | ≥ 85/100 |
| Publish / Linear-sync error rate | < 2% |
| Cost per article (LLM + image + infra) | < $0.50 |
| Calendar coverage maintained | ≥ 4 weeks scheduled ahead at all times, visible in Linear |

## 4. Non-Goals

- Social media distribution (future phase)
- Multi-language localization (future phase)
- Editing/updating existing legacy blog content
- Automated publishing to any CMS/storefront — publishing is always a manual act performed from the Linear issue, not a pipeline stage

## 5. Users / Stakeholders

- **Primary:** Store owners / marketing ops running content-driven SEO strategy, who review and publish from Linear
- **Secondary:** Agencies managing content for multiple clients, each with their own Linear team/project
- **Internal:** Content ops reviewing flagged/low-confidence drafts surfaced as "Needs Adjustments" in Linear

## 6. Pipeline Architecture

```
[Weekly Calendar Agent] → [Content Calendar: rolling N-week queue]
            ↓ (each new slot mirrored immediately as a Linear issue: Backlog, due date)
            ↓ (daily trigger pulls entries due today)
[Outline Agent] → [Draft Agent] → [SEO Optimization Agent] → [Image Generation]
   → [QA/Guardrail Check] → [route on QA outcome]
        ├─ confident pass + Shopify configured → [Publish live to Shopify]
        │        → [Linear issue → Done + live URL]
        └─ everything else → [Sync to Linear: Ready to Review / Needs Adjustments / Blocked]
   → [LangSmith Trace Log]
            ↑
   (Topic Research Agent runs inside the Calendar Agent to fill queue slots,
    not re-invoked per article at publish time)

QA confidence is the only gate: a confident pass auto-publishes; anything
uncertain routes to Linear for a human to review and publish by hand. There
is no execution-blocking approval interrupt. A publish failure is non-fatal —
the article still syncs to Linear (Needs Adjustments) with the error noted.
With Shopify unconfigured, every article takes the Linear-only branch.
```

### Stage breakdown

**0. Content Calendar Agent (runs weekly)**
- Checks current calendar coverage against target window (e.g., always keep 4 weeks scheduled ahead)
- If no seed keywords are configured, invokes the **Seed Keyword Research Agent** to brainstorm them from the niche alone — so a cold-start refresh needs nothing but a niche description
- Invokes the **Topic Research Agent** to fill empty slots up to the coverage target
- Dedupes candidates against already-synced articles and existing calendar entries (topic + keyword overlap check)
- Assigns each new topic a scheduled publish date/time based on configured cadence (e.g., Mon/Wed/Fri, 3x/week)
- Creates a Linear issue per new topic (`Backlog` state, due date = scheduled date, `Blog` label) so the calendar is visible and reorderable in Linear immediately
- Writes the updated calendar to the data store
- Sends a weekly digest (Slack/email) summarizing added topics, so a human can reorder, swap, or veto before drafting begins
- Idle weeks (queue already full) are a no-op — the agent only tops up what was consumed

**Seed Keyword Research Agent** *(invoked by the Calendar Agent when seed keywords are empty)*
- Input: niche/vertical, optional competitor URLs
- Output: a diverse list of realistic seed keywords (broad category + long-tail intent) via a single LLM call
- Feeds directly into the Topic Research Agent's `seed_keywords` input — no DataForSEO/SERP calls of its own

**Topic Research Agent** *(invoked by the Calendar Agent, not standalone)*
- Inputs: niche/vertical, target keywords, competitor URLs
- Tools: web search, SERP API, Google Trends API
- Output: ranked topic candidates with search volume, difficulty, and content gap analysis
- LangChain: ReAct agent with search + SERP tool calling

**1. Daily Draft Trigger**
- Cron (daily) queries the calendar for entries scheduled for today
- Feeds each due entry into the drafting pipeline below
- Entries with no due items today = no-op (this is what makes cadence, not just frequency, configurable)

**2. Outline Agent**
- Generates H2/H3 structure targeting the primary keyword + 3–5 secondary keywords
- Validates against top-ranking competitor structures (scraped headers)

**3. Draft Agent**
- LLM call via Google AI Studio (`gemini-3.5-flash`) with outline + brand voice guide + word count target
- Structured output: title, meta description, body (HTML), alt text per image slot

**4. SEO Optimization Agent**
- Checks keyword density, readability (Flesch score), internal link coverage (from the Shopify catalog's collections/pages)
- Generates `seo.title` and `seo.description` meta fields
- Scores the GEO (Generative Engine Optimization) levers from Aggarwal et al., "GEO: Generative Engine Optimization" (KDD 2024): presence of a quotable pull-quote (+41% AI citation rate in the study), named authoritative sources (+30%), and the fraction of `<h2>` sections landing in the ~150-400 word chunk band AI retrieval systems split pages on
- Below `SEO_MIN_SCORE`, one automatic revision pass targets the specific rubric weaknesses (including GEO ones) before the article moves on

**5. Image Generation**
- Generate featured + inline images via OpenRouter (`google/gemini-3.1-flash-lite-image` — token-billed, fractions of a cent/image)
- Upload the generated bytes to Linear's own file storage (`fileUpload`, `makePublic: true`) and embed the resulting public URL inline in the Linear description — no third-party image host needed

**6. QA / Guardrail Check**
- Fact-check pass via Google AI Studio (`gemini-3-flash`) — flags unverifiable claims
- Plagiarism/duplicate-content check (against previously drafted titles + the store's published articles)
- Brand safety check (tone, banned topics/claims)
- Confidence score + verdict determine the routing at stage 7 — auto-publish vs. Linear-only, and which Linear state the issue lands in

**7. Publish routing (auto-publish or Linear handoff)**
- **Confident pass + Shopify configured** → publish the article **live to Shopify** (`articleCreate`, `isPublished: true`), then update the calendar entry's Linear issue to the published state (`Done`) with the live URL recorded — a done-record, not a to-do.
- **Everything else** (low confidence, verdict `review`/`block`, or Shopify unconfigured) → sync the full draft to the Linear issue (body as Markdown, SEO meta, images, QA notes, raw-HTML block) in state `Ready to Review` / `Needs Adjustments` / `Blocked`, for a human to review and publish by hand.
- A publish failure is non-fatal: the article falls back to the Linear handoff (`Needs Adjustments`, error noted) so no work is lost.

**8. Observability**
- Every run traced in LangSmith: latency per stage, token cost, failure points
- Dashboard: articles published, articles synced, avg SEO score, cost/article, publish/sync error rate, calendar coverage (weeks ahead)

## 7. Technical Requirements

| Component | Choice |
|---|---|
| Orchestration | LangChain (agent graph) or LangGraph for stateful flow |
| Tracing/Eval | LangSmith |
| LLM gateway | Google AI Studio (single API key, OpenAI-compatible endpoint) — see Section 12 for model assignments |
| Linear integration | Linear GraphQL API, personal/workspace API key — calendar + draft handoff, no publish scope needed |
| Image gen | Gemini image models via OpenRouter, hosted on Linear's file storage — see Section 12 |
| SEO/SERP data | DataForSEO Standard Queue — see Section 12 |
| Data store | Postgres or Airtable for content calendar + run history |
| Trigger/scheduling | Cron (GitHub Actions / n8n) or serverless scheduled function |

## 8. Data Model (simplified)

```
Article {
  id
  topic_source: (manual | auto-researched)
  target_keywords: []
  outline: {}
  draft_html
  seo_title
  seo_description
  images: []
  qa_confidence_score
  status: (draft | synced | failed)
  linear_issue_id
  linear_identifier
  linear_url
  synced_at
  cost_usd
  trace_id (LangSmith)
}
```

```
ContentCalendar {
  id
  cadence: (e.g. "3x/week: Mon/Wed/Fri")
  coverage_target_weeks: int
  last_refreshed_at
}

CalendarEntry {
  id
  calendar_id (FK)
  scheduled_date
  topic
  target_keywords: []
  source: (auto-researched | manually added)
  status: (queued | drafting | drafted | skipped)
  linear_issue_id       # created the moment the slot is queued
  linear_identifier
  linear_url
  article_id (FK → Article, once drafting begins)
}
```

## 9. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| LLM hallucinated facts/claims | Mandatory QA fact-check stage; confidence threshold routing to "Needs Adjustments" |
| SEO cannibalization (duplicate topics) | Dedup check against existing synced topics before draft stage |
| Linear API rate limits / outage | Best-effort sync with retry/backoff; a failed sync marks the Article `failed` locally without losing the draft, so it can be retried |
| Brand voice drift at scale | Voice guide injected into every draft prompt; periodic human audit sample |
| Low-quality content reaching a live storefront | Not applicable by construction — the pipeline has no publish capability at all; every draft lands in Linear for human review first |
| Calendar drifts stale (agent fails silently, queue empties) | Monitor "weeks of coverage remaining" as a health metric; alert if it drops below 1 week |
| Weekly refresh piles up near-duplicate topics over time | Semantic similarity check (embeddings) against last N months of calendar + synced topics, not just exact-match dedup |

## 10. Phased Rollout

- **Phase 1:** Manual topic input → auto draft → sync to Linear (validate core pipeline)
- **Phase 2:** Add Content Calendar Agent — weekly auto-refresh of a rolling topic queue with dedup, mirrored to Linear as it's built, replacing manual per-post topic input
- **Phase 3:** Add SEO optimization layer + image generation
- **Phase 4:** Full QA guardrails with confidence-based Linear-state routing
- **Phase 5:** Everything up to Linear sync fully autonomous; calendar review and per-article publish in Linear remain the only regular human touchpoints — by design, not as a gate to eventually remove

## 12. Final Model Stack & Cost Estimate

*As of July 2026. LLM stages run on Google AI Studio's free tier — rate-limited, not billed. Verify current model names/limits at [aistudio.google.com](https://aistudio.google.com) before relying on this for capacity planning; free-tier quotas change without much notice.*

### 12.1 Model assignment (final)

All LLM calls route through **Google AI Studio's OpenAI-compatible endpoint**
using a single `GOOGLE_API_KEY`:

```
Base URL: https://generativelanguage.googleapis.com/v1beta/openai/
```

Since that endpoint is OpenAI-compatible, LangChain's `ChatOpenAI` class works
as a drop-in by pointing `base_url` at Google and swapping the `model` string
per stage — same pattern as the prior OpenRouter setup, no separate SDK.

| Stage | Model | Free-tier limit | Why |
|---|---|---|---|
| Calendar Agent / Topic Research | `gemini-3.1-flash-lite-preview` | generous, high rpm | Weekly-ish volume; separate bucket from Outline/SEO's `-lite` |
| Outline Agent | `gemini-3.1-flash-lite` | 500 req/day, 15 rpm | Runs every article; needs the generous quota, not the top model |
| Draft Agent | `gemini-2.5-flash` | 20 req/day, 5 rpm | Quality is reader-visible here, and this is the model verified reliably available (see note) |
| SEO Optimization Agent | `gemini-3.1-flash-lite` | 500 req/day, 15 rpm | Rule-checking + meta polish; shares the outline stage's high-quota model |
| QA / Fact-check / Guardrail | `gemini-3-flash-preview` | 20 req/day, 5 rpm | Last line of defense before a draft reaches Linear; kept on a **separate** 20/day bucket from Draft so the two don't compete |
| Image generation | `google/gemini-3.1-flash-lite-image` via OpenRouter | — (paid, near-zero cost) | Cheapest confirmed image-output model on OpenRouter's live catalog; AI Studio's own free tier returned `429` (no free image quota) |
| SEO/SERP research data | DataForSEO (Standard Queue) | — (paid) | Batch/overnight jobs — no need for real-time latency |

**Note on model names (verified live, 2026-07):** `gemini-3-flash` doesn't
exist — the real id carries a `-preview` suffix. `gemini-3.5-flash` and its
alias `gemini-flash-latest` both returned `503 UNAVAILABLE` (over capacity)
across repeated direct probes against `GET /v1beta/models` for a live key —
that's why Draft defaults to `gemini-2.5-flash` rather than the newest model.
Re-check `GET https://generativelanguage.googleapis.com/v1beta/models` for
your own key before trusting any model string here; the catalog moves fast
and free-tier availability isn't the same as the catalog listing existing.

**Why five different Gemini models instead of one:** AI Studio's free tier
rate-limits per model, not per account — the non-Lite Flash models are each
capped at **20 requests/day** independently. Running every stage on one
model would mean 4–5 LLM calls per article all draining the *same* 20/day
bucket, leaving almost no headroom for a second article the same day,
retries, or manual testing. Spreading stages across distinct models turns
one shared 20/day ceiling into several independent ones. The `-flash-lite`
variants (500 req/day) are deliberately used for the highest-volume,
lowest-stakes calls (calendar/research, outline, SEO polish) so those never
come close to any cap.

Gemma is available too — on this key's live catalog that means Gemma 4
(`gemma-4-26b-a4b-it`, `gemma-4-31b-it`), not Gemma 3 as older docs/tables
may still say — with a much larger request-per-day cap, but it's not used by
default: it's an open-weight model served
without confirmed reliable support for the structured-output / tool-calling
contract every stage here depends on (`.with_structured_output()`). Worth
revisiting as a high-volume fallback if the Flash-family quotas prove too
tight in practice — but verify structured-output reliability first.

### 12.2 Cost per article

**$0 in LLM spend** on the free tier — cost is bounded by rate limits, not
billing, so there's no per-token number to compute. The `CostTracker` in
`llm.py` still runs (for latency/token visibility in LangSmith), but
`MODEL_RATES` is intentionally empty, so `cost_usd` reports as `0.0`.

| Stage | Cost |
|---|---|
| LLM stages (research, outline, draft, SEO, QA) | $0.00 (free tier, rate-limited) |
| Images (2–3, `gemini-3.1-flash-lite-image` via OpenRouter, token-billed) | ~$0.005–0.01 |
| SERP/keyword calls (DataForSEO) | ~$0.01 |
| **Total per article** | **~$0.02** |

Well under the $0.50/article target in Section 3.

### 12.3 Monthly cost at typical cadence

At 3 posts/week (~12 articles/month): **~$0.24/month** in AI spend (images +
SERP only — this also requires a small OpenRouter credit top-up for images,
minimum ~$0.80, which will cover months at this volume). This excludes fixed
infra (hosting, DB, orchestration compute, Slack notifications), which will
dominate the actual bill at this volume.

### 12.4 When to reconsider

- **Move to a paid Gemini API tier (or Vertex AI)** once daily article volume
  or manual testing regularly bumps into the 20-requests/day caps on the
  Draft/QA/research models — no code change needed beyond raising the
  `GOOGLE_API_KEY` quota and filling in `MODEL_RATES` in `llm.py` for real
  cost tracking.
- **Try Gemma 4** (`gemma-4-26b-a4b-it`) for the outline/SEO stages if the 3.1
  Flash-Lite quota ever becomes the bottleneck, once structured-output
  reliability is confirmed against it.
- **Upgrade image generation** if featured images need to hold up as
  hero/campaign visuals rather than supporting blog imagery — `openai/gpt-5-image`
  or `google/gemini-3-pro-image` on OpenRouter are the next steps up in
  quality (and cost) from the current `-flash-lite-image` default.

## 13. Open Questions

- Image generation is on OpenRouter's cheapest Gemini image model for now — worth upgrading to a higher-quality model (or switching to real stock photos, e.g. Pexels) once featured images need to hold up as hero/campaign visuals rather than supporting blog imagery?
- Is internal linking worth reintroducing via a maintained sitemap now that there's no live product catalog to query?
- What's the retry/backoff behavior if the Linear API is down when a run tries to sync — how many attempts before it's left `failed` for manual resync?
- Per-client brand voice: shared prompt template vs. fine-tuned per client?
