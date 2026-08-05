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

## Phase 0 — Foundation

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

**Milestone:** open the app, see yesterday's GSC totals from the DB.

---

## Phase 1 — Monitoring dashboard

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

## Phase 2 — Blog management

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

## Phase 3 — Experiments (A/B testing)

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

### Route A: Windsor MCP (reporting, fast)

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
2. Programmable Search Engine + API key (Phase 5).
3. Decide the first `show-price` product set — recommended: accessories
   (underlay, stair nose, transitions, moulding; $25–30 commodity items
   where a visible price wins the click).

## Non-goals

- No hosting/remote access, no multi-user auth.
- No paid SERP/data subscriptions.
- No per-visitor split testing (impossible in this stack; see Phase 3).
- No fabricated structured data, ever — schema must match the visible page.
