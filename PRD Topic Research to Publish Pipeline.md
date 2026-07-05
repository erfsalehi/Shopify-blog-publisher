# PRD: Topic Research → Publish Pipeline (Shopify Blog Automation)

**Owner:** Erfan Salehi
**Status:** Final — Ready to Build
**Last updated:** July 2026

---

## 1. Problem Statement

Manual blog content production for Shopify stores is slow and inconsistent: topic selection is ad hoc, SEO research is manual, drafting takes hours, and publishing requires a human to log in and format each post. This creates a bottleneck for stores that need consistent content velocity (e.g., 3–5 posts/week) to compound SEO gains.

## 2. Goal

Build an agentic pipeline that autonomously researches topics, drafts SEO-optimized articles, generates supporting images, and publishes directly to Shopify — with human review as an optional gate, not a bottleneck.

## 3. Success Metrics

| Metric | Target |
|---|---|
| End-to-end time per article (trigger → published) | < 15 min |
| Human intervention required per article | 0 (fully auto) or 1 approval click (gated mode) |
| SEO score (on-page, via internal rubric) | ≥ 85/100 |
| Publishing error rate | < 2% |
| Cost per article (LLM + image + infra) | < $0.50 |
| Calendar coverage maintained | ≥ 4 weeks scheduled ahead at all times |

## 4. Non-Goals

- Social media distribution (future phase)
- Multi-language localization (future phase)
- Editing/updating existing legacy blog content
- Human-in-the-loop long-form editorial workflows (this is for automation-first content, not flagship editorial pieces)

## 5. Users / Stakeholders

- **Primary:** Store owners / marketing ops running content-driven SEO strategy
- **Secondary:** Agencies managing content for multiple Shopify clients
- **Internal:** Content ops reviewing flagged/low-confidence outputs

## 6. Pipeline Architecture

```
[Weekly Calendar Agent] → [Content Calendar: rolling N-week queue]
            ↓ (daily trigger pulls entries due today)
[Outline Agent] → [Draft Agent] → [SEO Optimization Agent] → [Image Generation]
   → [QA/Guardrail Check] → [Human Approval Gate (optional)]
   → [Shopify Publish] → [LangSmith Trace Log]
            ↑
   (Topic Research Agent runs inside the Calendar Agent to fill queue slots,
    not re-invoked per article at publish time)
```

### Stage breakdown

**0. Content Calendar Agent (runs weekly)**
- Checks current calendar coverage against target window (e.g., always keep 4 weeks scheduled ahead)
- Invokes the **Topic Research Agent** to fill empty slots up to the coverage target
- Dedupes candidates against already-published articles and existing calendar entries (topic + keyword overlap check)
- Assigns each new topic a scheduled publish date/time based on configured cadence (e.g., Mon/Wed/Fri, 3x/week)
- Writes the updated calendar to the data store
- Sends a weekly digest (Slack/email) summarizing added topics, so a human can reorder, swap, or veto before drafting begins
- Idle weeks (queue already full) are a no-op — the agent only tops up what was consumed

**Topic Research Agent** *(invoked by the Calendar Agent, not standalone)*
- Inputs: niche/vertical, target keywords, competitor URLs
- Tools: web search, SERP API, Google Trends API
- Output: ranked topic candidates with search volume, difficulty, and content gap analysis
- LangChain: ReAct agent with search + SERP tool calling

**1. Daily Publish Trigger**
- Cron (daily) queries the calendar for entries scheduled for today
- Feeds each due entry into the drafting pipeline below
- Entries with no due items today = no-op (this is what makes cadence, not just frequency, configurable)

**2. Outline Agent**
- Generates H2/H3 structure targeting the primary keyword + 3–5 secondary keywords
- Validates against top-ranking competitor structures (scraped headers)

**3. Draft Agent**
- LLM call via OpenRouter (`anthropic/claude-sonnet-5`) with outline + brand voice guide + word count target
- Structured output: title, meta description, body (HTML), alt text per image slot

**4. SEO Optimization Agent**
- Checks keyword density, readability (Flesch score), internal linking opportunities
- Auto-inserts internal links from a maintained sitemap/product catalog
- Generates `seo.title` and `seo.description` fields for Shopify

**5. Image Generation**
- Generate or source featured + inline images
- Upload via Shopify `stagedUploadsCreate` → attach to article

**6. QA / Guardrail Check**
- Fact-check pass via OpenRouter (`anthropic/claude-opus-4.8`) — flags unverifiable claims
- Plagiarism/duplicate-content check
- Brand safety check (tone, banned topics/claims)
- Confidence score determines auto-publish vs. human review routing

**7. Human Approval Gate (configurable)**
- Slack/email notification with preview link
- Approve / Edit / Reject actions

**8. Shopify Publish**
- `articleCreate` GraphQL mutation with all fields populated
- Sets `publishedAt` (immediate, since the calendar already handled scheduling)

**9. Observability**
- Every run traced in LangSmith: latency per stage, token cost, failure points
- Dashboard: articles published, avg SEO score, cost/article, error rate, calendar coverage (weeks ahead)

## 7. Technical Requirements

| Component | Choice |
|---|---|
| Orchestration | LangChain (agent graph) or LangGraph for stateful flow |
| Tracing/Eval | LangSmith |
| LLM gateway | OpenRouter (single API key, OpenAI-compatible endpoint) — see Section 12 for model assignments |
| Shopify integration | Admin GraphQL API, custom app, `write_content` + `write_files` scopes |
| Image gen | Flux Schnell (via fal.ai or Replicate) — see Section 12 |
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
  status: (draft | pending_review | approved | published | failed)
  shopify_article_id
  published_at
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
  status: (queued | drafting | drafted | published | skipped)
  article_id (FK → Article, once drafting begins)
}
```

## 9. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| LLM hallucinated facts/claims | Mandatory QA fact-check stage; confidence threshold routing |
| SEO cannibalization (duplicate topics) | Dedup check against existing published topics before draft stage |
| Shopify API rate limits | Queue + backoff; batch scheduling instead of burst publishing |
| Brand voice drift at scale | Voice guide injected into every draft prompt; periodic human audit sample |
| Over-automation reputational risk | Configurable human gate; start gated, graduate to full-auto per client |
| Calendar drifts stale (agent fails silently, queue empties) | Monitor "weeks of coverage remaining" as a health metric; alert if it drops below 1 week |
| Weekly refresh piles up near-duplicate topics over time | Semantic similarity check (embeddings) against last N months of calendar + published topics, not just exact-match dedup |

## 10. Phased Rollout

- **Phase 1:** Manual topic input → auto draft → auto publish (validate core pipeline)
- **Phase 2:** Add Content Calendar Agent — weekly auto-refresh of a rolling topic queue with dedup, replacing manual per-post topic input
- **Phase 3:** Add SEO optimization layer + image generation
- **Phase 4:** Full QA guardrails with confidence-based routing
- **Phase 5:** Full autonomy end-to-end; human gate becomes exception-only, calendar review becomes the only regular touchpoint

## 12. Final Model Stack & Cost Estimate

*Pricing as of July 2026 — verify against provider pages before committing budget; rates shift often (Claude Sonnet 5 is on introductory pricing through Aug 31, 2026, reverting to $3/$15 after).*

### 12.1 Model assignment (final)

| Stage | Model | Why |
|---|---|---|
| Calendar Agent (weekly gap analysis) | Claude Haiku 4.5 | Pattern-matching/dedup task — no reasoning premium needed |
| Topic Research Agent | Claude Haiku 4.5 | Same — summarizing SERP/trends data |
| Outline Agent | Claude Haiku 4.5 | Structural task, well within Haiku's ability |
| Draft Agent | Claude Sonnet 5 | Quality is reader-visible here — this is where spend matters |
| SEO Optimization Agent | Claude Haiku 4.5 | Rule-checking (density, links, meta fields) |
| QA / Fact-check / Guardrail | Claude Opus 4.8 | Last line of defense before publish — reasoning quality has the most leverage here |
| Image generation | Flux Schnell | Good-enough output for blog featured/inline images at near-zero cost |
| SEO/SERP research data | DataForSEO (Standard Queue) | Batch/overnight jobs — no need for real-time latency |

All LLM calls route through **OpenRouter** using a single `OPENROUTER_API_KEY`:

```
Base URL: https://openrouter.ai/api/v1
Models:
  anthropic/claude-haiku-4.5    (research, outline, SEO)
  anthropic/claude-sonnet-5     (draft)
  anthropic/claude-opus-4.8     (QA/fact-check)
```

Since OpenRouter's API is OpenAI-compatible, LangChain's `ChatOpenAI` class works as a drop-in by pointing `base_url` at OpenRouter and swapping the `model` string per stage — no separate SDK per provider, and LangSmith tracing works unchanged since it wraps the LangChain call, not the transport.

**Cost note on OpenRouter:** it passes through Anthropic's own per-token rates with no markup on inference — the only fee is 5.5% on credit-card top-ups (min $0.80), or 5% on crypto. Load credits in one larger top-up rather than many small ones to avoid the minimum-fee tax on small purchases. The cost table below includes this 5.5% on the LLM portion.

### 12.2 Cost per article

Assumes a ~1,500-word article: ~2,000 input / 2,500 output tokens for the draft step, smaller footprints for research/outline/SEO/QA, plus 2–3 images.

| Stage | Cost |
|---|---|
| Topic research (Haiku 4.5, amortized share) | ~$0.0045 |
| Outline (Haiku 4.5) | ~$0.0035 |
| Draft (Sonnet 5) | ~$0.029 |
| SEO optimization (Haiku 4.5) | ~$0.0055 |
| QA / fact-check (Opus 4.8) | ~$0.025 |
| **LLM subtotal** | **~$0.068** |
| OpenRouter credit fee (5.5% of LLM subtotal) | ~$0.004 |
| Images (2–3, Flux Schnell) | ~$0.03 |
| SERP/keyword calls (DataForSEO) | ~$0.01 |
| **Total per article** | **~$0.11** |

Well under the $0.50/article target in Section 3 — even with the OpenRouter fee added, the LLM stage stays cheap because only the draft and QA steps use paid-tier reasoning.

### 12.3 Monthly cost at typical cadence

At 3 posts/week (~12 articles/month): **~$1.35/month** in AI spend (LLM + images + SERP). This excludes fixed infra (hosting, DB, orchestration compute, Slack/email notifications), which will dominate the actual bill at this volume — AI spend itself is close to a rounding error.

### 12.4 When to reconsider

- **Escalate the draft step to Opus 4.8 or Claude Fable 5** for cornerstone/pillar content where traffic value justifies the extra ~$0.04–0.15/article.
- **Move off OpenRouter to direct Anthropic billing** if monthly LLM spend grows large enough that 5.5% becomes a meaningful line item (rough breakeven is in the low thousands of dollars/month) — until then, the single-key convenience across three model tiers outweighs the fee.
- **Upgrade image generation** if featured images need to hold up as hero/campaign visuals rather than supporting blog imagery — Flux 2 Pro or GPT Image 1.5 at ~$0.045–0.055/image is the next step up.

## 13. Open Questions

- Which image generation provider balances cost vs. brand consistency?
- Should internal linking use a static sitemap or live product catalog query?
- What's the fallback behavior if Shopify API publish fails after QA approval?
- Per-client brand voice: shared prompt template vs. fine-tuned per client?
