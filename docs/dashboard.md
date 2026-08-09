# Control Center — the dashboard

A web app over `drflooring.ca`, built on top of the `blog_pipeline` package
rather than beside it. Phase 0 of [PLAN.md](../PLAN.md) is done: the
foundation, the job framework, and the Search Console daily sync.

```bash
pip install -e ".[dashboard]"
python -m dashboard
```

Then open <http://127.0.0.1:8600>. The first thing to do is the Jobs page →
**Run now** on the Search Console sync; nothing is on the Overview until that
has run once.

It also runs deployed, at <https://dr-flooring-control-center.vercel.app>,
behind a password — see [Running on Vercel](#running-on-vercel). Most of this
document describes behaviour common to both; where they differ, that section
says so.

## The one rule

**Sync jobs talk to external APIs. Pages read only the database.**

Not a performance preference. This machine's outbound requests go through a
local proxy at `127.0.0.1:3067` that intermittently answers 429
(`local_rate_limited`) or drops TLS mid-handshake, so a page that fetched live
data would fail unpredictably and for reasons the owner can do nothing about.
It's also the only way price history and experiment baselines can exist at all
— both need yesterday's numbers *kept*, not re-fetched.

`tests/test_dashboard_web.py::test_no_page_makes_an_outbound_request` asserts
it, because the rule is easy to break with one convenient import and the
breakage is invisible until a bad morning.

## Layout

```
src/dashboard/
  __main__.py      python -m dashboard  (loopback only; refuses other hosts)
  config.py        machine-level settings (port, db path) — credentials stay
                   in .env and are read via blog_pipeline's Settings
  models.py        tables; db.py: engine/session (SQLite, WAL)
  store.py         owner-editable settings, declared once as Specs
  reporting.py     every read query the UI makes
  charts.py        inline SVG line charts, server-rendered
  scheduler.py     APScheduler, reads its times from store.py
  jobs/
    registry.py    what a job is
    runner.py      job_run logging, retry/backoff, per-job lock
    gsc.py         Search Console daily sync
  web.py           FastAPI routes; templates/ and static/
```

Two databases, deliberately. The pipeline owns `data/pipeline.db` (Postgres in
CI); the dashboard owns `data/dashboard.db`. Sharing one would force the
dashboard's schema to migrate in lockstep with a database it doesn't control.
Both are gitignored.

## Settings

Machine-level settings are env vars prefixed `DASHBOARD_` (`DASHBOARD_PORT`,
`DASHBOARD_DATABASE_URL`, `DASHBOARD_ENABLE_SCHEDULER`). Everything else —
sync windows, chunk sizes, the nightly hour, the competitor list — lives in the
database and is edited on the Settings page, because editing `.env` and
restarting is not a settings page.

Each setting is declared once in `store.py` as a `Spec` with its bounds and
help text, and the form renders itself from that. An unset setting reads its
declared default and writes nothing, so a fresh install has a complete working
configuration and an empty `app_setting` table.

## Jobs

Every job is idempotent and resumable, because the runner retries the whole
function when the proxy misbehaves. A job that appended rather than upserted
would corrupt its own data the first time a retry fired mid-run.

The retry policy is narrow on purpose. 429, 5xx, transport errors and
SSL/EOF/reset text are retried with jittered backoff (2s, 4s, 8s). Everything
else — notably a **403** from Search Console, which means the service account
was never added to the property — fails immediately, because retrying a real
error just delays a message the owner needs to read. A `tries` count above 1 on
a *successful* run is visible on the Jobs page: that's the proxy, and the retry
working.

### `gsc_daily`

Pulls per-day site totals (`dimensions=["date"]`) and per-URL rows
(`["date", "page"]`). This is separate from the pipeline's own
`sync-performance`, which stores two 90-day windows to rank decaying posts and
by construction cannot draw a trend line.

- **First run backfills** 180 days by default. On the real store that was 27
  API calls, ~141k rows, 93 seconds.
- **Later runs re-fetch only** the trailing restatement window: 3 calls, ~10
  seconds.
- Per-URL rows are fetched a week at a time and **committed per chunk**, so an
  interrupted backfill keeps what it already got.
- Coverage is tracked in a `gsc_fetch_day` ledger rather than inferred from
  stored rows — a day with genuinely zero impressions returns nothing, and
  inferring would re-fetch every quiet day forever.
- A chunk that hits the row cap is stored but **not** marked fetched, and says
  so in the run detail. Partial data is worth showing; freezing it isn't.

## The settling window

Search Console restates its last ~3 days. Two consequences the UI handles
explicitly, because ignoring either manufactures a decline that isn't
happening:

- Both comparison windows on the Overview **end on settled data**
  (`today - 3`), never on a partial tail.
- The trend charts draw unsettled days **dashed and muted**, with the boundary
  marked and the tooltip saying "provisional".

## Arithmetic that's easy to get wrong

Enforced in `reporting.py` and pinned by tests:

- **CTR is recomputed from totals, never averaged.** A mean of daily CTRs
  weights a 9-impression day the same as a 9,000-impression one.
- **Position is impression-weighted**, as Google's own average position is. An
  unweighted mean silently disagrees with the number in Search Console's UI,
  which is the fastest possible way to lose trust in a dashboard.
- **Position is the one metric where down is good.** Arrows follow the number,
  colour follows the meaning.

## No JS libraries

Charts are server-rendered inline SVG and the only client script is ~50 lines
of vanilla `fetch` that starts a job and polls until it finishes. Vendoring
Chart.js and htmx would mean committing downloaded bundles to draw a line
through thirty numbers and to avoid twenty lines of `fetch`. Say the word if
you'd rather have the real libraries.

### `shopify_catalog`

Every product's handle, title, type, vendor, status, price, inventory and
tags, paged 250 at a time and written per page. Real run: 2,786 products, 12
pages, 12 seconds.

Products are upserted by Shopify's global id and carry `first_seen` /
`last_seen` rather than being deleted and reinserted. A product that
disappears is interesting — delisted, or the sync only got half the pages —
and a row that silently vanishes can tell you neither. `not_seen_this_run` in
the job detail is the count.

A `price_min` of 0.00 is not missing data: it's the Orichi hide-price app
doing its job, and it's what the show-price rollout will change. 93 of 2,786
products currently show a real price.

### `ga4_daily`

Daily sessions/users/engaged sessions, plus per-event counts for the events
named in settings. **Those events are the store's bottom line** — nothing is
checked out on this site, so a phone call is the outcome, and GA4 is the only
system that can see one.

Two corrections to what PLAN.md assumed, both measured against the live
property on 2026-08-05 rather than guessed:

- **`call_for_price_click` does not exist.** It has never fired.
- **`click_phone_number` is a duplicate of `call_click`** — identical counts
  on all 26 days with any activity, i.e. two GTM tags on one click. Counting
  both would double every phone conversion. Only `call_click` is in the
  default list.

The default event list is therefore `call_click, whatsapp_click,
directions_click`. A configured event GA4 has never seen is reported in the
run detail as `events_not_found_in_ga4`, because otherwise a typo presents as
"conversions: 0" — indistinguishable from a bad month.

Also worth knowing: `phone_call` (24 events) stopped firing around
2026-07-17, and the whole conversion-tag set only starts on 2026-07-04. Any
comparison reaching before that date is measuring when tracking was installed,
which the Ads page says out loud rather than rendering as a 418% rise.

### `ads_windsor` (Phase 4A)

Campaign spend, clicks, impressions and conversions from Windsor.ai's REST
API — available now, without waiting on the Google Ads developer token.

**Connecting Windsor's MCP connector does not configure this.** That connector
is a tool inside Claude's session; a job running at 07:00 on this machine
cannot reach it. Same Windsor account, different door. The job needs
`WINDSOR_API_KEY` in `.env`, from
<https://onboard.windsor.ai/app/data-preview>.

Every row records its `source`. When the direct Google Ads API lands, both
pipes will run during the changeover, and a Windsor sync deliberately deletes
only its own rows. A spend figure whose origin is ambiguous is a spend figure
nobody trusts.

Conversions are stored as floats: Google attributes partial conversions and
this account really does report 1.5 and 2.5.

### `blog_articles`

The only job that reads a database rather than an API: it copies article
metadata and refresh history out of the pipeline's own store. Read-only,
always — the pipeline stays the sole writer.

Copied rather than joined across, because the Blog page needs to match
articles to *this* database's daily Search Console rows (a cross-database join
happens in Python either way), and because the pipeline's store is SQLite
locally but Postgres in CI. Real run: 78 articles, 73 live, 3 ever refreshed.

It also surfaces the known cost-tracking bug rather than letting `$0.00` read
as "the LLM calls were free".

### `alerts`

Evaluates every enabled rule and files what it finds. Also runs automatically
after every *other* job — a failed 06:00 sync should produce its alert at
06:00, not at 08:00 — with the scheduled entry as a backstop for a day when
nothing else ran.

## Alerts

Six rule kinds: organic clicks drop, average position slips, call/WhatsApp
events drop, ad spend with no conversions, cost per conversion too high, and
a sync job failing. Thresholds are rows, not code, so tuning one at 7am
doesn't need an edit and a restart.

Three properties matter more than the rules themselves:

- **Deduplication is on rule + subject, never the date.** An early version
  included the evaluated date in the fingerprint, which mints a fresh row
  every night — exactly the duplication the fingerprint exists to prevent. A
  condition that persists is one row with a rising `times_seen`.
- **"Acknowledged" means until it changes, not until tomorrow.** Each run
  records which fingerprints still fire; anything absent is marked
  `resolved_at`. A finding against a resolved alert reopens it and pings
  again; a finding against an acknowledged-but-still-present one just bumps
  the counter. Without that distinction, acknowledging either hides a problem
  forever or lasts a day.
- **A problem that fixes itself clears the inbox.** Most do. An inbox that
  only grows is one nobody opens.

Nothing is evaluated against unsettled days, for the same reason the Overview
comparison windows stop short of them.

Notifications go to Slack via the webhook the pipeline already has configured
— not a Windows toast, which is gone the moment it's dismissed and invisible
when the machine is locked, which is most of the time this runs. `notify` is
off per rule by default: an alert nobody asked to be interrupted by is how
alerting gets muted wholesale. The inbox is the system of record regardless.

## Pages

`/` overview · `/products` · `/blog` · `/ads` · `/alerts` · `/jobs` ·
`/settings`. The open-alert count rides in the nav on every page, because an
alerting system you have to remember to go and look at is one you stop
looking at.

**Blog** ranks by **absolute impressions lost**, not percent — the same choice
`run-refresh` makes, and for the same reason. Percentage flatters trivia: a
post falling 25→1 is a 96% collapse worth 24 impressions, while 18,272→5,497
is "only" −70% and worth 12,775. A post is only counted as decaying if it had
at least 50 impressions to fall from: one that never had impressions isn't
decaying, it's new or it's invisible, and those want different responses.

**Products** joins the catalogue to per-URL search metrics. The join is done
in Python because both sides need URL normalisation (scheme, `www`, trailing
slash, query string, case) and SQLite has no expression index to make that
cheap in SQL. At 2,786 products it's a few milliseconds, and the
normalisation lives in exactly one function instead of duplicated into a
query. 2,031 of 2,786 products currently match search data — a join that
matched nothing would look identical to a catalogue with no traffic, so
`test_products_join_search_metrics_despite_url_variation` pins the four
variations Google actually emits.

Sorting by position pushes zero-impression products to the back: position 0
means "never shown", not "ranked first".

**Experiment cohorts** live in `product_cohort`, not in Shopify tags. A tag is
public surface that can leak into a storefront filter and can be removed in
admin by someone who doesn't know what it meant; membership also has to stay
frozen for the life of an experiment. Nothing in the live catalogue encodes
the existing SEO pilot's 10/50 split, so it has to be entered once on the
Products page.

## Refreshing an article (the only path that changes a public page)

`/blog/<id>` → **Preview a refresh** runs `run_refresh(only_ids={id},
dry_run=True)`: the pipeline's own graph, prompts and asset guards, calling
the model but writing nothing. The proposal is stored, and the page shows a
block-level diff of what a reader would see.

**Approve publishes the stored HTML**, not a fresh run. That distinction is
the whole point: re-running on approval would call the model again and publish
something nobody read, while the change summary still said "shortened the
intro". So the dashboard owns the write — and therefore re-checks every guard
at apply time, against the *current* live body rather than trusting the
preview:

1. **Unchanged since preview.** The live body is fingerprinted at preview
   time. If someone edited the post in Shopify admin, or the Wednesday cron
   refreshed it, applying would silently revert them — so it refuses and asks
   for a fresh preview.
2. **No dropped assets**, via the pipeline's own `lost_assets`.
3. **No leaked `[IMAGE - ...]` placeholder.**

The previous body is snapshotted to `ArticleRevision` before the write, in its
own committed transaction — the only undo Shopify offers for a published post.
`blog-pipeline rollback-refresh --article-id N` restores it.

Nothing schedules an apply. Every one is a person clicking approve on a diff.

The diff compares block-level prose rather than raw HTML: these bodies are
often one enormous line, and even split by tag the review drowns in attribute
noise. Blocks are matched on normalised text, so re-wrapped whitespace and
regenerated JSON-LD don't read as content changes — which is precisely why the
asset guards are enforced in code rather than left to the reviewer's eye.

### On the `$0.00` cost

PLAN.md called this an instrumentation bug. It isn't. `llm.MODEL_RATES` is
empty deliberately — AI Studio's free tier is rate-limited, not billed — so
zero dollars is the correct dollar cost. The real gap was that `CostTracker`
measured input and output tokens and then discarded them before any caller saw
them. `run_refresh` now returns both, and every refresh proposal records them,
because tokens against the daily cap are what actually constrains a run. A
real refresh of one article costs roughly 2,000 in / 4,600 out.

## Experiments

`/experiments`. Per-visitor A/B testing is impossible on this stack — search
shows one title to everyone, Shopify can't price differently per visitor — so
the feature is cohort + time based, and says so on the page rather than
implying otherwise.

**Difference-in-differences.** Δtreatment − Δcontrol. The control group
absorbs whatever happened to the whole site in the window (a seasonal dip, an
algorithm update), so what's left is attributable to the change. A
before-and-after on the treatment group alone would credit the change with the
season — `test_a_site_wide_lift_is_not_credited_to_the_treatment` pins that.

**Significance from a permutation test**, not a t-test. Group labels are
shuffled 5,000 times to see how often chance produces an effect this large.
With 10 products against 50 there's no reason to assume normally distributed
per-product deltas, and a t-test's p-value would carry more confidence than
the data supports. Seeded, so the same data always scores the same.

**It refuses to give a verdict** below 5 products per group, or when too many
members have no impressions in either window. A difference measured across
three products is a story about three products.

**The baseline is frozen once** at start and never recomputed. A baseline
derived live would drift as Search Console's windows moved, letting a test
quietly produce whichever answer was hoped for. Membership freezes at the same
moment, for the same reason.

**Controls are matched on impressions**, not picked at random, and never
auto-committed. A product nobody sees can't show a seasonal dip, so a control
set of invisible products silently turns the comparison back into a
before-and-after.

### Writing the treatment

Only `title` and `description` are written by the app. Price changes are made
by hand in admin — this app should not be driving a store's price field — and
the experiment still scores them, since the baseline is frozen regardless of
how the change was made.

Two Shopify behaviours are handled in `product_seo.py`, both of which fail
*silently* (mutation reports success, stores something else):

- **`seo` is replaced wholesale, not merged.** Sending only `seo.title` blanks
  the existing description. Both fields go on every write, with the unchanged
  one read back first.
- **An `seo.title` equal to the product title is discarded** — Shopify stores
  null with no error. For a title experiment that's the entire treatment
  silently not applied, so it's rejected up front.

Every write verifies Shopify's echo rather than trusting empty `userErrors`,
and one product failing doesn't abort the rest of the cohort — half a cohort
applied with no record of which half is the worst possible state.

### The `show-price` prerequisite

Price experiments need visible prices. See
[show-price.md](show-price.md) for the runbook: the Orichi tag exclusion, the
exact theme patch, the 47 already-priced accessories to tag first, and the
validation steps. One tag drives both the app's exclusion list and the theme's
JSON-LD `offers`, so markup and page cannot drift.

## Keywords

`/keywords`. Search Console query rows joined to DataForSEO market data.

Search Console says how the site performs on a term and structurally cannot
know how big the term is or what a click is worth. DataForSEO knows the second
and nothing about the first. Neither is actionable alone — a term with 40,000
searches the site ranks 90th for is a fantasy; a term it ranks 6th for that
nobody searches is a rounding error.

**Striking distance** is positions 5-40 with at least 20 impressions: Google
already considers the site relevant, so one better page is a far shorter path
to page one than a high-volume term with no history. **Opportunity** ranks by
impressions already earned weighted by the click-through being left behind —
computed from Search Console alone, deliberately *not* from market volume,
because volume is missing until it's paid for and a ranking built on a
mostly-absent column just ranks which terms happened to be looked up.

### Spending money carefully

DataForSEO bills **per request, not per keyword**, and accepts up to 1,000
terms per call. Batching is the entire cost strategy: 700 keywords for $0.09
instead of 700 x $0.09.

- A **spend cap** (`dfs.budget_usd`, default $0.50) is checked against this
  app's own `api_spend` ledger before any call. The prepaid balance was $1.00
  when this was built — about eleven requests.
- A **rejected call records no spend.** DataForSEO charges nothing for one,
  and inventing a charge is worse than under-counting.
- The job **never retries** (`max_attempts=1`). The client returns `[]` rather
  than raising, so a retry would be a second charge for the same unanswered
  question.
- Terms are re-fetched only after 45 days. Volume is a 12-month rolling
  average; asking weekly buys nothing.

Two findings from wiring this up:

- **`location_code` was 2840 — the United States.** The pipeline had been
  pulling US search volumes for a business serving Langley, BC. Default is now
  2124 (Canada).
- **The account is unverified**, so every call returns `40104` and HTTP 403.
  `DataForSEOClient` now records `last_error`, so the Keywords page shows
  DataForSEO's own message instead of a guess. Verify at
  <https://app.dataforseo.com/> to turn the volume column on.

## The advisor

A per-tab note plus a tracked checklist, on `/`, `/products`, `/keywords`,
`/blog`, `/ads` and `/experiments`.

The failure mode it is designed against is not bland advice. It is **a
confident paragraph containing a number that does not exist** — next to
columns of real ones, an invented figure inherits their credibility.

- **The model sees only a brief built from real rows** (`advisor_context.py`).
  No tools, no fetching. That brief is the complete universe of figures
  available to it.
- **The brief is stored on the note** and shown under "the exact data the
  model was given". A suggestion you can't trace to its numbers is an
  assertion.
- **The output is checked against the brief.** `unverified_figures` extracts
  substantial numbers — 3+ digits, or carrying a currency symbol — and flags
  any not present. Small counts are ignored on purpose ("the top 3 titles"),
  because flagging them would bury the real catches. It annotates rather than
  blocks: the model legitimately derives percentages, so a flag means *check
  this*, not *this is wrong*.
- **Memory is outcomes, not transcripts.** Each suggestion becomes an
  `advisor_action` you mark done or dismissed, and those outcomes go into the
  next prompt. That lets it follow up on what was actually done and stop
  repeating what was rejected — which re-reading old notes cannot do.

Generated on demand and by a weekly job that only refreshes scopes whose note
is over a week old. Never on page load: the free tier is rate-limited, and
advice that changes every refresh is advice nobody trusts.

### Which model actually works

Being listed in `GET /v1beta/models` is not entitlement. Measured against this
key on 2026-08-08:

| Model | Free tier |
|---|---|
| `gemini-3-pro-preview` | **`limit: 0`** — cannot be called |
| `gemini-3.1-pro-preview` | **`limit: 0`** — cannot be called |
| `gemini-3-flash-preview` | works (the default) |
| `gemini-2.5-flash` | works |
| `gemini-3.1-flash-lite` | works |

Both Pro models appear in the catalog and return 429 with a zero quota for
requests *and* input tokens. So a configured model that turns out to be
unusable falls through `LLM_FALLBACK_MODELS`, and the note records which model
actually answered rather than silently producing nothing.

## Running on Vercel

The app is deployed at
<https://dr-flooring-control-center.vercel.app>. Everything below is what had
to change for the same code to run there as well as on loopback — it still
runs locally exactly as described above, unchanged.

**Entrypoint.** [`api/index.py`](../api/index.py) puts `src/` on `sys.path`
and imports `dashboard.web:app`. Vercel's Python runtime detects the ASGI
`app` and serves every route through it; there is no separate router config.

**Dependencies.** [`requirements.txt`](../requirements.txt), not
`pyproject.toml` — and `pyproject.toml` is in `.vercelignore` so the build
can't pick it up instead. The list is the transitive set of imports the
dashboard actually reaches, which is much smaller than the pipeline's
(no langgraph, no fastembed and its ~45MB of onnxruntime, no textstat, no
typer). Add exactly what a new import needs rather than switching to the
full set.

**Database.** Neon Postgres, connected through Vercel's Storage tab, which
auto-populates `DATABASE_URL`. `DashboardSettings.database_url` reads
`DASHBOARD_DATABASE_URL` first and falls back to `DATABASE_URL` /
`POSTGRES_URL`, so the click-to-connect integration works without knowing
this app's naming. `db.py` normalises the URL to `postgresql+psycopg://` and
switches to `NullPool` — a serverless function that pools connections just
leaks them, since the process dies between requests.

The local default stays SQLite. Keep `DASHBOARD_DATABASE_URL` pinned in
`.env`, because the pipeline sets its own `DATABASE_URL=sqlite:///data/pipeline.db`
and the fallback would otherwise silently point the dashboard at the
pipeline's database.

**Scheduling.** APScheduler cannot work here: no process lives long enough to
hold a timer, so it would start, get torn down, and fire nothing — silent, not
loud. `enable_scheduler_effective` forces it off whenever `VERCEL=1`,
regardless of configuration, and the jobs run instead as Vercel Cron hitting
`/api/cron/<job>` on the schedule in [`vercel.json`](../vercel.json). Those
endpoints authenticate with `Authorization: Bearer $CRON_SECRET`, which Vercel
sends automatically for cron-triggered requests — separate from the owner's
password, because cron has no browser and no session cookie to present.

Note the Hobby plan runs cron **once a day** with up to ~59 minutes of jitter,
so the times in `vercel.json` are ordering, not appointments.

**Access.** The app was local-only by design; reachability *was* the access
control. Public means that's gone, so `DASHBOARD_PASSWORD` gates every route
by default (see [auth.py](../src/dashboard/auth.py)) with an
`itsdangerous`-signed session cookie. Leave the password empty locally and
nothing changes. Set `DASHBOARD_SESSION_SECRET` alongside it — installing the
middleware without one raises rather than generating a per-process secret,
which would log the owner out on every cold start.

### Trailing newlines will take the app down

`vercel env add` reads the value from **stdin**, so `echo "$v" | vercel env add`
stores `v\n`. This bit twice:

- On `CRON_SECRET`, Vercel's own build refused it outright — it can't sit in an
  HTTP header. Loud, easy.
- On `DASHBOARD_ENABLE_SCHEDULER`, pydantic's bool parser doesn't strip before
  matching `"true"`/`"false"`, so `'false\n'` raised a `ValidationError` inside
  `get_settings()` — at import time, before any route existed. Every URL
  returned 500, login page included, and the traceback pointed at the database
  line that ran *earlier* in a previous deploy, which made it look like a
  Postgres problem for far longer than it should have.

Pipe with `< file` rather than `echo |`, and write the file with
`newline=''`. `config.py` also strips at the settings boundary now — both the
secrets and the boolean — so a contaminated value can't reach the parser
again.

```bash
printf 'value' > /tmp/v && vercel env add NAME production < /tmp/v && rm /tmp/v
```

Env vars are read at **build** time, so changing one needs a redeploy before
the running function sees it.

## Not built yet

Experiments and their scoring (Phase 3 — needs the `show-price` mechanism; no
product carries that tag yet), the direct Google Ads API and its management
actions (Phase 4B), competitor price watch (Phase 5, Collector A only — see
PLAN.md). The `competitor` table and its settings UI exist now so the list is
owner-managed from the start rather than hardcoded later.

The pipeline's existing Wednesday blog-refresh cron is untouched — PLAN.md has
it migrating in later, and a half-moved scheduler is worse than two.
