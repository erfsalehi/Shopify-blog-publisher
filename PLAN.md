# D&R Flooring Control Center — Build Plan

A local dashboard app to manage drflooring.ca: monitor products and site stats
(GA4 + Search Console), manage blog posts, run A/B-style experiments on
titles/prices/strategies, manage Google Ads, and watch competitor prices with
alerts.

Decisions already made by the owner — do not re-litigate:

- **Local-only.** No hosting, no remote access, no auth layer. Runs on the
  same Windows machine as the existing pipeline and cron.
- **No paid SERP API.** Competitor SERP data comes from the free path in
  Phase 5 (Google Programmable Search Engine + our own price extraction).
- **Google Ads: both routes.** Windsor MCP for quick reporting AND direct
  Google Ads API for management. Setup instructions in Phase 4 — start the
  developer-token application early; approval is the calendar bottleneck.
- **Competitor sites are specified inside the app**, not hardcoded. The
  owner adds/removes them from a settings page.

This plan builds on the existing `blog_pipeline` package — Shopify Admin
GraphQL client (`src/blog_pipeline/tools/shopify.py`), GSC + GA4 credentials
already wired (`GSC_CREDENTIALS_JSON`, `GA4_PROPERTY_ID` in `.env`), fastembed
embeddings (`blog_pipeline.dedup._embed` / `_cosine`), and the refresh graph
(`src/blog_pipeline/graphs/refresh_graph.py`). It is a layer over that code,
not a rewrite.

---

## Environment constraints (read first)

- **Windows 11, PowerShell.** Existing repo is Python.
- **A local HTTP proxy at `127.0.0.1:3067`** intermittently returns HTTP 429
  (`local_rate_limited`) or SSL EOF on outbound requests, especially to the
  storefront. This is why the architecture stores everything locally: sync
  jobs retry with backoff; the UI never blocks on a live API call.
- **Secrets:** all in `.env` at repo root. Never print the service-account
  private key; never copy `.env` into tracked paths. A backup already exists
  in the session scratchpad.
- **Store facts:** ~2,769 products; 94% priced 0.00 with prices hidden by the
  Orichi hide-price app ("Call for price" → tel:+16045322211). Conversion is
  a phone call. ~5 products have Judge.me review metafields. Product JSON-LD
  is gated on real reviews in `sections/main-product.liquid`.

---

## Phase 0 — Foundation ✅ built

Shipped as `src/dashboard/` — see [docs/dashboard.md](docs/dashboard.md).
`python -m dashboard` serves on 127.0.0.1:8600. Two deviations from the stack
below, both to avoid committing downloaded JS bundles to a repo on a machine
with a flaky proxy: charts are **server-rendered inline SVG** rather than
Chart.js, and the one interactive control uses ~50 lines of vanilla `fetch`
rather than htmx. Everything else is as written.

**Stack**

- **FastAPI** app serving JSON API + server-rendered UI (Jinja2 + htmx;
  charts via a small JS lib, e.g. Chart.js vendored locally).
- **SQLite** (`dashboard.db`, gitignored) — every external pull lands in
  tables with `fetched_at`. **The UI reads only the DB, never live APIs.**
  This defeats the proxy flakiness, respects quotas, keeps pages fast, and
  is a hard requirement for experiments and price history anyway.
- **APScheduler** in-process for recurring jobs (daily GA4/GSC sync, weekly
  SERP checks, competitor fetches). The existing Wednesday blog-refresh cron
  can migrate in later; don't break it in Phase 0.
- Package as `src/dashboard/` importing `blog_pipeline` as a library. One
  entry point: `python -m dashboard` → serves on `http://127.0.0.1:<port>`.

**Deliverables**

1. App skeleton, DB schema/migrations (plain SQL or SQLAlchemy), settings
   page storing app-managed config (competitor list, watchlists, alert
   rules) in the DB.
2. First working sync job: GSC daily pull (site totals + per-URL) into DB.
3. Job-run log table + a page showing last run / status / errors for every
   job (essential given the proxy).

**Milestone:** open the app, see yesterday's GSC totals from the DB. ✅
First real backfill: 179 days, 140,852 per-URL rows, 27 API calls, 93s. Second
run re-fetched only the restatement window: 3 calls, 10s, zero duplicate rows.

---

## Phase 1 — Monitoring dashboard — ✅ built

All four pages (Site overview, Products with the SEO-pilot cohort view, Blog
posts with decay flags, Alerts v1) and all syncs. Alerts notify via the
already-configured Slack webhook rather than a Windows toast — a toast is gone
when dismissed and invisible when the machine is locked, which is most of the
time this runs. See [docs/dashboard.md](docs/dashboard.md).

Two measured corrections to the assumptions below:

- **`call_for_price_click` does not exist** in the GA4 property — it has never
  fired. The events that do: `whatsapp_click` (45), `call_click` (41),
  `directions_click` (16), and `phone_call` (24, but it stopped around
  2026-07-17 — worth checking why).
- **`click_phone_number` is an exact duplicate of `call_click`** (identical
  counts on all 26 active days — two GTM tags on one click). Counting both
  doubles every phone conversion, so only `call_click` is counted.
- The conversion tags only start on **2026-07-04**, so any comparison reaching
  further back measures when tracking was installed. The Ads page says so
  rather than rendering the resulting 418% rise as good news.

**Data syncs (daily)**

- GSC: totals, per-URL, per-query (top N) — clicks, impressions, CTR,
  position.
- GA4: sessions, engagement, key events. Call clicks exist as GTM events
  (`call_click`, `call_for_price_click`, `whatsapp_click`) — surface these
  as the store's real conversions.
- Shopify: product snapshot (id, handle, title, price, tags, status,
  inventory) — needed by every later phase.

**Pages**

1. **Site overview** — trend lines, week-over-week deltas, top movers.
2. **Products** — all products with search metrics per product URL,
   sortable/filterable. Include a permanent **SEO pilot view**: the
   10-product treatment vs 50-product control cohort (pilot started
   ~2026-07; comparison due mid-late August 2026). Cohort membership lives
   in the DB.
3. **Blog posts** — per-article GSC performance, decay flag (reuse the
   `SearchPerformance` two-window comparison from the refresh pipeline),
   refresh history, last-refreshed date.
4. **Alerts v1** — threshold rules stored in DB (position drop > X, clicks
   down > Y% WoW), evaluated after each sync, shown in an alerts inbox.
   Notification channel: Windows toast or email (SMTP creds in `.env`) —
   owner's choice at build time.

---

## Phase 2 — Blog management — ✅ built

Blog list plus a per-article page with preview → diff → approve → publish.
Two things below turned out differently:

- **"Approve" publishes the previewed HTML, not a second run.** Re-running
  `run_refresh` on approval would call the model again and publish something
  the owner never read — the change summary would still say "shortened the
  intro" and the article would be different. The proposal's HTML is stored and
  that is what gets written, with all three guards re-checked at apply time
  against the *current* live body: unchanged-since-preview, no dropped assets,
  no leaked `[IMAGE - ...]` marker.
- **The `$0.00` cost is not an instrumentation bug.** `llm.MODEL_RATES` is
  empty on purpose (see its comment): AI Studio's free tier is rate-limited,
  not billed, so zero dollars is the correct figure. The genuine gap was that
  `CostTracker` measured input/output tokens and threw them away —
  `run_refresh` now returns them and each proposal records them, since tokens
  against the daily cap are the resource actually constrained.

Two small additive changes to `blog_pipeline` were needed:
`run_refresh(only_ids=...)` to target one named article (bypassing the
selector cooldown, which is a scheduling heuristic and not a safety guard),
and `original_html`/`proposed_html` on dry-run results so a diff can be shown.

UI over the existing pipeline functions:

- Article list: status, cooldown state, decay score, last refresh.
- Per-article: trigger dry-run, view proposed diff, approve → publish
  (calls `run_refresh` with the article id; the `skip_ids` mechanism
  becomes checkboxes).
- Show the Linear issues the pipeline creates for image suggestions.
- Surface the cost-tracking numbers (note: currently reports $0.00 —
  instrumentation bug in the pipeline, fix while here).

Low risk, low effort: no new external integrations.

---

## Phase 3 — Experiments (A/B testing) — ✅ framework built

`/experiments`: create, matched controls, freeze baseline, apply, verdict.
Scoring is difference-in-differences as described below, with significance
from a **permutation test** rather than a t-test — with 10 products against
50 there is no reason to believe per-product deltas are normally distributed,
and a t-test's p-value would carry more confidence than the data supports.
Shuffling group labels 5,000 times assumes nothing and needs no scipy. Below
5 products per group it refuses to give a verdict at all.

The `show-price` prerequisite is written up in
[docs/show-price.md](docs/show-price.md), including the exact patch for the
live theme's `sections/main-product.liquid` (read from the running theme, so
it matches). Three findings from that work:

- **Inventory tracking is off store-wide** (`tracksInventory: false`, all
  2,786 products report 0) while `availableForSale` is true. So the markup
  takes availability from `product.available`, never a quantity — quoting the
  count would mark every product OutOfStock, which is worse than no markup.
- **47 accessories already carry real prices** ($12–$30 reducers, stair
  noses, T-mouldings), so the first `show-price` set needs only tagging, no
  admin price entry. `python -m dashboard.tools.show_price_candidates` lists
  them.
- **One product is priced $0.39** (Underlay - Memory Foam Vinyl Underlayment)
  — almost certainly per-square-foot or an error. Excluded from the
  recommended set; publishing it as a product price would be wrong in public.

Also confirmed: the custom app **does** hold `write_products`, so title and
description experiments can be applied by the app. `write_theme_code` is
granted too, but the theme patch is left for a human to paste into a
duplicated theme — a live theme edit is not something this app should do
unattended.

## Phase 3 — original design notes

**Honest framing baked into the design:** per-visitor split tests are not
possible here — search shows one title to everyone, and Shopify can't show
different prices to different visitors. The industry-standard alternative
for SEO, and the structure the owner already used for the SEO pilot, is
**cohort + time-based testing with difference-in-differences scoring.**
The feature is an experiment framework:

1. **Create experiment:** name, hypothesis, variable (`title` /
   `description` / `price` / `strategy` free-form), treatment product set,
   control set (app proposes controls matched on impressions ± embedding
   similarity; owner confirms), start date, duration.
2. **Baseline snapshot** frozen at start (from DB history).
3. **Apply**: for titles/descriptions the app writes via `productUpdate`
   — **must send both `seo.title` and `seo.description` every time**
   (Shopify replaces the whole `seo` object; and it silently drops a
   `seo.title` equal to the product title — returns null with no
   userErrors). For prices, apply is manual in admin + tag; the app
   records the event.
4. **Score:** Δtreatment − Δcontrol over identical windows (CTR for title
   tests; calls/impressions for price tests). Verdict page with a small-N
   noise caveat — the app must say when a result is not significant.

**Price experiments depend on the show-price mechanism** (agreed in a
prior session, not yet built):

- Owner sets real prices in admin and tags products `show-price`.
- Orichi hide-price app: enable tag exclusion for `show-price` (its config
  already supports `excludeProductTags`; currently off/empty).
- Theme: extend the gated Product JSON-LD in
  `sections/main-product.liquid` to also emit when
  `product.tags contains 'show-price' and product.price > 0`, adding a
  truthful `offers` object (price from `product.price | divided_by:
  100.0`, currency from `cart.currency.iso_code`, availability from
  `product.available`). Untagged products keep today's behaviour exactly.
- Rule that must never break: **markup must match the visible page.**
  One tag drives both the app and the schema so they cannot drift.

---

## Phase 4 — Google Ads (both routes)

### Route A: Windsor (reporting, fast) — ✅ built, needs a key

Built as the `ads_windsor` job. Two corrections to what's written below,
both found on 2026-08-05 with the account connected:

- **The MCP is not the integration.** Windsor's MCP connector is a tool inside
  Claude's session; a scheduled job on this machine cannot reach it. The job
  uses Windsor's REST API (`connectors.windsor.ai`) and needs
  `WINDSOR_API_KEY` in `.env`, from onboard.windsor.ai/app/data-preview.
- **Windsor is not reporting-only.** It exposes write actions on Google Ads —
  `pause_campaign`, `enable_campaign`, `set_campaign_budget`,
  `push_negative_keywords`, `set_target_cpa`, `set_max_cpc` and more. So the
  Phase 4B management feature set is reachable today. The direct API is still
  worth having (free, no vendor in the path), but it is no longer a blocker
  for management.

Account confirmed connected: D&R Flooring, `690-753-6043`. Three live
campaigns — `Leads-Performance Max- July 25`, `Store Goal PMax` (both PMax)
and `phone camp aug 2025` (Search).

- Windsor.ai connector; the owner connects their Google Ads account in
  Windsor's UI, then the MCP exposes spend/clicks/conversions.
- Check Windsor's current free-tier limits at signup; if the free tier is
  too tight for daily pulls, fall back to weekly pulls or the direct API
  below (which is free).
- Dashboard job pulls campaign-level metrics into the DB; shown next to
  organic + calls, which is the view Google's own UI never gives.
- **Reporting only** — no management via Windsor.

### Route B: Direct Google Ads API (management)

Setup instructions for the owner — do these steps early, approval takes days:

1. **Create a Google Ads Manager (MCC) account** at ads.google.com/home/tools/manager-accounts
   (separate from the regular ads account; free). Link the existing D&R
   ads account (the one behind `AW-686101589`) to it.
2. In the MCC: **Tools → API Center → apply for a developer token.**
   It starts at test-account-only access; **apply for Basic access** via
   the form (describe use: "internal reporting and campaign management
   dashboard for our own single store"). Basic allows managing your own
   linked accounts and is free.
3. **OAuth client:** in Google Cloud project `ytsearch-495619` (already
   used for GSC/GA4), enable the **Google Ads API**, create an OAuth 2.0
   client ID (Desktop app). Note: the Ads API does *not* accept the
   service account for a normal ads account — it needs OAuth with the
   owner's Google login. Run the one-time flow to obtain a **refresh
   token** (google-ads lib ships a helper script).
4. `.env` additions: `GOOGLE_ADS_DEVELOPER_TOKEN`,
   `GOOGLE_ADS_CLIENT_ID`, `GOOGLE_ADS_CLIENT_SECRET`,
   `GOOGLE_ADS_REFRESH_TOKEN`, `GOOGLE_ADS_LOGIN_CUSTOMER_ID` (the MCC id).
5. Python: `pip install google-ads`.

**Dashboard features once live:** campaign/ad-group/keyword report sync
(GAQL), spend-vs-calls page, and management actions — pause/enable
campaigns, budget changes, add negative keywords — each behind an explicit
confirm dialog and written to an audit log table.

---

## Phase 5 — Competitor price watch (free path)

Two collectors, one comparison engine, all config in-app.

### Collector A: named competitor sites

- Settings page: owner adds competitor base URLs (and optionally specific
  product/collection URLs). Stored in DB.
- Weekly (configurable) fetch of competitor product pages. Extraction
  order: (1) JSON-LD `Product`/`offers` — most flooring retailers run
  Shopify/Woo and emit it; (2) OpenGraph `product:price:amount`;
  (3) per-site CSS selector the owner can set in the app when a site
  resists (2 minutes of setup beats a scraping arms race).
- **Product matching:** app proposes matches to our catalog via fastembed
  title embeddings + attribute hints (mm thickness, series names, SKU
  fragments); owner confirms/rejects in a review queue. Confirmed matches
  persist. (Auto-matching flooring SKUs across brands is genuinely hard;
  human-confirm-once is the reliable design. Note: numpy `float32` is not
  JSON-serializable — wrap scores in `float()`.)
- Politeness: respect robots.txt, 1 req/few seconds per host, identify
  with a plain UA. These are public product pages; keep it boring.

### Collector B: Google SERP, first 2 pages — free

> **⚠️ This route is closed as of 2026-08-05 — verify before building.**
> Google's own docs now state: *"The Custom Search JSON API is closed to new
> customers"*, and existing customers *"have until January 1, 2027 to
> transition to an alternative solution"*
> ([source](https://developers.google.com/custom-search/v1/overview)). A new
> `CSE_API_KEY` almost certainly cannot be obtained, and even a grandfathered
> one dies in under 18 months — not worth building against.
>
> **Two-minute check before writing this off:** in Cloud project
> `ytsearch-495619`, try APIs & Services → Library → "Custom Search API" →
> Enable. If the project is grandfathered it will enable; if it's closed the
> option won't be available.
>
> **Recommended fallback:** ship Phase 5 as **Collector A only**. It was
> always the more reliable half — named competitors, direct fetch, JSON-LD
> extraction, human-confirmed product matching — and the owner already
> supplies the competitor list from the settings page. What's lost is
> *discovery* of competitors we didn't name, which for a local flooring
> market is a short, stable, already-known list. A paid SERP API remains the
> upgrade path and remains out of scope.

- **Google Programmable Search Engine (Custom Search JSON API)** — free
  100 queries/day, legitimate and stable. Setup (owner, ~10 min):
  1. programmablesearchengine.google.com → create engine → "Search the
     entire web".
  2. Copy the engine id (`cx`).
  3. In Cloud project `ytsearch-495619`: enable **Custom Search API**,
     create an API key.
  4. `.env`: `CSE_API_KEY`, `CSE_CX`.
- Each watched product = 1 query, 2 pages = 2 API calls (10 results each).
  Budget: 100 free calls/day → **up to 50 watched products checked daily**,
  or the whole priced catalog on a rotating schedule. The app tracks daily
  usage and rotates automatically.
- Query template: `"<series/product name>" flooring price` with `gl=ca`;
  optionally append `langley OR surrey OR vancouver` — honest limitation:
  CSE has no true geo-targeting and **no Google Shopping results**, so
  "best price in my area" = best price among (a) named local competitors
  from Collector A and (b) any SERP result whose page yields a price.
  Prices come from fetching the result URLs and running the same JSON-LD
  extraction as Collector A — not from Google itself. If deeper Shopping
  coverage ever matters, a paid SERP API is the upgrade path; explicitly
  out of scope now.

### Comparison + alerts

- Daily job: for each watched product with a real price, compare our price
  vs lowest confirmed competitor price. When we're not lowest: alert with
  product, our price, their price, delta, and the competitor URL.
- Watchlist = all `show-price` tagged products by default (today that's
  ~nothing — this phase is downstream of the pricing rollout) plus any
  manually added products.
- Price history table → per-product price-over-time chart, ours vs theirs.

---

## Build order

```
Phase 0 ──> Phase 1 ──> Phase 2
                │
                ├──> Phase 3  (needs show-price mechanism; build it here)
                ├──> Phase 4  (owner starts token application immediately;
                │              Windsor first, direct API when approved)
                └──> Phase 5  (needs CSE key + show-price rollout)
```

Owner actions that gate later phases — start now, they cost calendar time
not build time:

1. Google Ads MCC + developer token application (Phase 4B).
2. ~~Programmable Search Engine + API key (Phase 5).~~ **Dead — the Custom
   Search JSON API closed to new customers and shuts down 2027-01-01. See the
   warning box in Phase 5.** Replaced by: name the competitor sites on the
   settings page, which needs no key and no calendar time.
3. Decide the first `show-price` product set — recommended: accessories
   (underlay, stair nose, transitions, moulding; $25–30 commodity items
   where a visible price wins the click).

## Deployed — ✅ built

Superseded the "no hosting" non-goal below. The dashboard runs at
<https://dr-flooring-control-center.vercel.app> on Vercel + Neon Postgres,
gated by a single owner password, with the sync jobs running as Vercel Cron
against `/api/cron/<job>` instead of the in-process scheduler (which cannot
survive in a serverless function). It still runs locally on loopback exactly
as before, unchanged and unauthenticated. Full write-up:
[docs/dashboard.md](docs/dashboard.md#running-on-vercel).

Still single-user — one shared password, not a users table for one person.

## Non-goals

- ~~No hosting/remote access~~ — now deployed, see above. Still no
  multi-user auth.
- No paid SERP/data subscriptions.
- No per-visitor split testing (impossible in this stack; see Phase 3).
- No fabricated structured data, ever — schema must match the visible page.
