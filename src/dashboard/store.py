"""The app-managed settings store.

Each setting is declared once as a `Spec` — key, type, default, and the label
and help text the settings page renders. Declaring them rather than hand-
writing a form means a new knob is one entry here and appears in the UI with
validation already attached, and `get()` can hand back a correctly typed value
whether or not the row exists yet.

Nothing is written to the database until the owner changes it. An unset key
reads its declared default, so a fresh install has a complete, working
configuration and an empty `app_setting` table.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from dashboard.db import get_session
from dashboard.models import AppSetting

# ── Setting keys ────────────────────────────────────────────────────
# Named constants rather than bare strings at the call sites: a typo in a
# string key is a silent default, which is the worst failure mode a settings
# system has.
GSC_BACKFILL_DAYS = "gsc.backfill_days"
GSC_RECENT_DAYS = "gsc.recent_days"
GSC_PAGE_CHUNK_DAYS = "gsc.page_chunk_days"
GSC_PAGE_ROW_LIMIT = "gsc.page_row_limit"
GSC_QUERY_BACKFILL_DAYS = "gsc.query_backfill_days"

DFS_LOCATION_CODE = "dfs.location_code"
DFS_MAX_KEYWORDS = "dfs.max_keywords_per_run"
DFS_BUDGET_USD = "dfs.budget_usd"

ADVISOR_MODEL = "advisor.model"
ADVISOR_MAX_ACTIONS = "advisor.max_actions"
JOB_HISTORY_KEEP = "jobs.history_keep"

# Per-job schedule keys follow one shape so `_schedule_specs` below can
# generate them and `JobSpec` can name them without a lookup table.
JOB_GSC_ENABLED, JOB_GSC_HOUR = "jobs.gsc_daily.enabled", "jobs.gsc_daily.hour"

GA4_BACKFILL_DAYS = "ga4.backfill_days"
GA4_RECENT_DAYS = "ga4.recent_days"
GA4_EVENTS = "ga4.conversion_events"

LOCAL_CITIES = "local.cities"
LOCAL_SEEDS = "local.seed_keywords"
LOCAL_BUDGET_USD = "local.budget_usd"
LOCAL_MAX_KEYWORDS = "local.max_keywords_per_run"

ADS_BACKFILL_DAYS = "ads.backfill_days"
ADS_RECENT_DAYS = "ads.recent_days"

IMPORT_MODEL = "import.model"
IMPORT_MAX_PRODUCTS = "import.max_products_per_collection"
IMPORT_BATCH = "import.products_per_pass"
IMPORT_MAX_IMAGES = "import.max_images_per_product"
IMPORT_MAX_DOCS = "import.max_docs_per_product"
IMPORT_PUBLISH_STATUS = "import.publish_status"
IMPORT_TAG_PREFIX = "import.source_tag"
IMPORT_BRAND_BLURB = "import.brand_blurb"
IMPORT_TOP_BANNER = "import.top_banner"
IMPORT_ALL_CHANNELS = "import.all_channels"
IMPORT_RELATED_LIMIT = "import.related_limit"



def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_int(value: Any) -> int:
    if isinstance(value, bool):  # bool is an int; never what was meant here
        raise ValueError("expected a number, got a checkbox value")
    return int(str(value).strip())


def _as_float(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("expected a number, got a checkbox value")
    return float(str(value).strip())


def _as_optional_str(value: Any) -> str:
    """Like `_as_str`, but empty is a real answer.

    Most string settings name something the app needs — a model, a tag — and
    blanking one is a mistake worth refusing. The brand block is the
    exception: emptying it means "add nothing to descriptions", which is a
    choice, not a typo.
    """
    return str(value).strip()


def _as_str(value: Any) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError("cannot be empty")
    return text


def _as_csv(value: Any) -> list[str]:
    """A comma-separated text field, stored as a JSON list.

    Text in, list out: asking the owner to type JSON into a settings form is
    how you get a settings form nobody edits.
    """
    if isinstance(value, list):
        items = [str(v).strip() for v in value]
    else:
        items = [v.strip() for v in str(value).split(",")]
    return [v for v in items if v]


@dataclass(frozen=True)
class Spec:
    key: str
    default: Any
    label: str
    help: str
    group: str
    kind: str  # "int" | "bool" | "str"
    coerce: Callable[[Any], Any]
    minimum: int | None = None
    maximum: int | None = None

    def clean(self, raw: Any) -> Any:
        value = self.coerce(raw)
        if isinstance(value, str):
            # For text, the bounds are a length. Comparing a string to an int
            # would raise TypeError from inside a settings save, which reads
            # as a bug in the form rather than as "that is too long".
            if self.maximum is not None and len(value) > self.maximum:
                raise ValueError(
                    f"{self.label} must be at most {self.maximum} characters"
                )
            return value
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"{self.label} must be at least {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise ValueError(f"{self.label} must be at most {self.maximum}")
        return value


SPECS: tuple[Spec, ...] = (
    Spec(
        key=GSC_BACKFILL_DAYS,
        default=180,
        label="Search Console backfill (days)",
        help=(
            "How far back the first sync reaches. Search Console keeps 16 "
            "months; this is a one-time cost paid on an empty database."
        ),
        group="Search Console",
        kind="int",
        coerce=_as_int,
        minimum=7,
        maximum=480,
    ),
    Spec(
        key=GSC_RECENT_DAYS,
        default=10,
        label="Search Console re-pull window (days)",
        help=(
            "Every sync re-fetches this many recent days and overwrites them. "
            "Google restates the last few days as its data settles, so a "
            "window shorter than about a week freezes wrong numbers in place."
        ),
        group="Search Console",
        kind="int",
        coerce=_as_int,
        minimum=3,
        maximum=90,
    ),
    Spec(
        key=GSC_PAGE_CHUNK_DAYS,
        default=7,
        label="Per-URL fetch chunk (days)",
        help=(
            "Per-URL rows are fetched a chunk of days at a time and written "
            "before the next chunk starts, so a run interrupted by the proxy "
            "resumes instead of restarting. Smaller chunks mean more API "
            "calls but less lost work; larger chunks risk hitting the row cap."
        ),
        group="Search Console",
        kind="int",
        coerce=_as_int,
        minimum=1,
        maximum=30,
    ),
    Spec(
        key=GSC_PAGE_ROW_LIMIT,
        default=60000,
        label="Per-URL row cap per chunk",
        help=(
            "Safety cap on rows fetched for one chunk. Google returns 25000 "
            "per request and the client pages past that, so this is the point "
            "at which the job stops and says it truncated rather than paging "
            "forever. A run that reports truncation needs a smaller chunk."
        ),
        group="Search Console",
        kind="int",
        coerce=_as_int,
        minimum=5000,
        maximum=500000,
    ),
    Spec(
        key=GA4_BACKFILL_DAYS,
        default=180,
        label="Analytics backfill (days)",
        help="How far back the first GA4 sync reaches.",
        group="Analytics",
        kind="int",
        coerce=_as_int,
        minimum=7,
        maximum=480,
    ),
    Spec(
        key=GA4_RECENT_DAYS,
        default=5,
        label="Analytics re-pull window (days)",
        help=(
            "GA4 keeps adjusting recent days as late-arriving events land. "
            "Shorter than this and the last few days stay wrong."
        ),
        group="Analytics",
        kind="int",
        coerce=_as_int,
        minimum=2,
        maximum=60,
    ),
    Spec(
        key=GA4_EVENTS,
        # Measured against the live property on 2026-08-05, not guessed.
        # Two corrections to what PLAN.md assumed:
        #   * `call_for_price_click` does not exist in this property at all.
        #   * `click_phone_number` is a DUPLICATE of `call_click` — identical
        #     counts on all 26 days with any activity, i.e. two GTM tags on
        #     one click. Including both would double every phone conversion,
        #     so only `call_click` is counted.
        # `directions_click` is kept as a separate line: it's store-visit
        # intent, not a call, and averaging the two would blur both.
        default=["call_click", "whatsapp_click", "directions_click"],
        label="Conversion events",
        help=(
            "Comma-separated GA4 event names treated as conversions. Nothing "
            "is bought on this site — a phone call is the outcome — so these "
            "GTM events are the store's real bottom line, not sessions. "
            "Careful adding to this list: click_phone_number duplicates "
            "call_click exactly, so counting both doubles your calls."
        ),
        group="Analytics",
        kind="csv",
        coerce=_as_csv,
    ),
    Spec(
        key=ADS_BACKFILL_DAYS,
        default=180,
        label="Google Ads backfill (days)",
        help="How far back the first Windsor pull reaches.",
        group="Google Ads",
        kind="int",
        coerce=_as_int,
        minimum=7,
        maximum=730,
    ),
    Spec(
        key=ADS_RECENT_DAYS,
        default=5,
        label="Google Ads re-pull window (days)",
        help=(
            "Google restates recent conversion counts as attribution windows "
            "close, and Windsor mirrors whatever Google currently says."
        ),
        group="Google Ads",
        kind="int",
        coerce=_as_int,
        minimum=2,
        maximum=90,
    ),
    Spec(
        key=GSC_QUERY_BACKFILL_DAYS,
        default=90,
        label="Search terms backfill (days)",
        help=(
            "Query rows are far more numerous than URL rows and the long tail "
            "is mostly one-impression noise, so they get their own shorter "
            "history. Capped by the main backfill setting."
        ),
        group="Search Console",
        kind="int",
        coerce=_as_int,
        minimum=14,
        maximum=480,
    ),
    Spec(
        key=DFS_LOCATION_CODE,
        default=2124,
        label="DataForSEO location code",
        help=(
            "2124 is Canada; 2840 is the United States. Search volume is "
            "market-specific, and the pipeline had been asking for US volumes "
            "for a business serving Langley, BC. Province-level codes exist "
            "(British Columbia is 20444) if you want tighter targeting."
        ),
        group="Keywords",
        kind="int",
        coerce=_as_int,
        minimum=1,
        maximum=9999999,
    ),
    Spec(
        key=DFS_MAX_KEYWORDS,
        default=700,
        label="Keywords per request",
        help=(
            "DataForSEO bills per request, not per keyword, and accepts up to "
            "1000 search terms in one call — so batching is the entire cost "
            "strategy. Kept under the limit to leave room for its own dedup."
        ),
        group="Keywords",
        kind="int",
        coerce=_as_int,
        minimum=10,
        maximum=1000,
    ),
    Spec(
        key=DFS_BUDGET_USD,
        default=0.50,
        label="DataForSEO spend cap (USD)",
        help=(
            "Total this app may ever spend. The job refuses to call once the "
            "ledger reaches it, so a scheduling mistake can't drain a prepaid "
            "balance overnight. Raise deliberately."
        ),
        group="Keywords",
        kind="float",
        coerce=_as_float,
        minimum=0,
        maximum=1000,
    ),
    Spec(
        key=ADVISOR_MODEL,
        default="gemini-3-flash-preview",
        label="Advisor model",
        help=(
            "Model used for the per-tab notes. Measured against this key on "
            "2026-08-08: gemini-3-pro-preview and gemini-3.1-pro-preview are "
            "listed in the catalog but return limit: 0 on the free tier — "
            "they cannot be called at all. The Flash models work. If a model "
            "here turns out to be unavailable the advisor falls back through "
            "LLM_FALLBACK_MODELS and records which one answered."
        ),
        group="Advisor",
        kind="str",
        coerce=_as_str,
    ),
    Spec(
        key=ADVISOR_MAX_ACTIONS,
        default=5,
        label="Suggestions per note",
        help=(
            "More than a handful and none of them get done. Each becomes a "
            "tracked item you can mark done or dismissed."
        ),
        group="Advisor",
        kind="int",
        coerce=_as_int,
        minimum=1,
        maximum=10,
    ),
    Spec(
        key=JOB_HISTORY_KEEP,
        default=200,
        label="Job runs kept per job",
        help="Older rows are pruned after each run so the log stays readable.",
        group="Schedule",
        kind="int",
        coerce=_as_int,
        minimum=10,
        maximum=5000,
    ),
)

LOCAL_SPECS: tuple[Spec, ...] = (
    Spec(
        key=LOCAL_CITIES,
        # Location codes confirmed against DataForSEO's own CA list, not
        # guessed. A wrong code silently returns a SERP for somewhere else,
        # which is the kind of error that looks exactly like data.
        default=[
            "Langley:9226804",
            "Langley Township:9072429",
            "Surrey:1001964",
            "Abbotsford:1001861",
        ],
        label="Cities to track rankings in",
        help=(
            "Name:location_code pairs, comma separated. Google shows a "
            "different SERP inside each city, and Search Console only ever "
            "reports one national average — so this is the only way to know "
            "where the site actually stands in Langley. Codes come from "
            "DataForSEO's /serp/google/locations/ca list."
        ),
        group="Local SEO",
        kind="str",
        coerce=_as_csv,
    ),
    Spec(
        key=LOCAL_SEEDS,
        default=[
            "flooring langley",
            "flooring store langley",
            "laminate flooring langley",
            "vinyl plank flooring langley",
            "hardwood flooring langley",
        ],
        label="Local keywords to always track",
        help=(
            "Tracked every run regardless of what Search Console reports, "
            "because the terms worth owning are often the ones the site "
            "doesn't rank for yet — and those have no impressions to be "
            "discovered from."
        ),
        group="Local SEO",
        kind="str",
        coerce=_as_csv,
    ),
    Spec(
        key=LOCAL_MAX_KEYWORDS,
        default=6,
        label="Keywords per rank-tracking run",
        help=(
            "Each keyword costs one SERP request per city, so this times the "
            "city count is the per-run bill. Six keywords across four cities "
            "is 24 requests, roughly $0.05."
        ),
        group="Local SEO",
        kind="int",
        coerce=_as_int,
        minimum=1,
        maximum=50,
    ),
    Spec(
        key=LOCAL_BUDGET_USD,
        default=2.0,
        label="Local rank tracking budget (USD, lifetime)",
        help=(
            "Its own cap, separate from the keyword budget — sharing one "
            "would let a keyword refresh quietly eat the month's rank "
            "tracking. The job refuses to call once this is reached."
        ),
        group="Local SEO",
        kind="int",
        coerce=_as_float,
    ),
)

SPECS = SPECS + LOCAL_SPECS

IMPORT_SPECS: tuple[Spec, ...] = (
    Spec(
        key=IMPORT_MODEL,
        default="~deepseek/deepseek-v4-flash-latest",
        label="Model that writes product copy",
        help=(
            "One call per product, and it is the call that decides what the "
            "page says about a product the store has to stand behind. Worth "
            "a stronger model than the dashboard's advisor uses — a "
            "rate-limited one falls back to a plain description built from "
            "the source, not to a guess. A name containing '/' (like the "
            "default) routes through OpenRouter and needs OPENROUTER_API_KEY; "
            "anything else routes through Google AI Studio and needs "
            "GOOGLE_API_KEY."
        ),
        group="Product import",
        kind="str",
        coerce=_as_str,
    ),
    Spec(
        key=IMPORT_MAX_PRODUCTS,
        default=60,
        label="Most products taken from one collection",
        help=(
            "A ceiling on how much one pasted URL can create. A manufacturer "
            "range is usually well under this; a URL that turns out to be "
            "the whole catalogue is the case this exists for."
        ),
        group="Product import",
        kind="int",
        coerce=_as_int,
        minimum=1,
        maximum=500,
    ),
    Spec(
        key=IMPORT_BATCH,
        default=3,
        label="Products handled per pass",
        help=(
            "Each product is a page fetch, a few PDF downloads, an LLM call "
            "and several Shopify mutations — 10-25 seconds of work. On "
            "Vercel the whole pass has to finish inside 60 seconds, so this "
            "is the number that keeps a run resumable instead of killed "
            "mid-product. Raise it when running locally."
        ),
        group="Product import",
        kind="int",
        coerce=_as_int,
        minimum=1,
        maximum=25,
    ),
    Spec(
        key=IMPORT_MAX_IMAGES,
        default=8,
        label="Most images per product",
        help=(
            "Shopify fetches each one from the manufacturer's CDN. Past "
            "half a dozen they are usually room scenes repeated across the "
            "whole range rather than this product."
        ),
        group="Product import",
        kind="int",
        coerce=_as_int,
        minimum=1,
        maximum=30,
    ),
    Spec(
        key=IMPORT_MAX_DOCS,
        default=4,
        label="Most documents per product",
        help=(
            "Spec sheet, installation guide, warranty, care guide — in that "
            "order of usefulness. Each is downloaded, read for its "
            "specifications, and re-hosted on the store."
        ),
        group="Product import",
        kind="int",
        coerce=_as_int,
        minimum=0,
        maximum=12,
    ),
    Spec(
        key=IMPORT_BRAND_BLURB,
        default='D&R Flooring and Renovations is on the Langley Bypass. We stock many <a href="https://drflooring.ca/collections/vinyl-flooring">Vinyl flooring</a>, <a href="https://drflooring.ca/collections/laminate-flooring">Laminate flooring</a>, <a href="https://drflooring.ca/collections/engineered-flooring">Engineered Hardwood</a>, <a href="https://drflooring.ca/collections/transition-and-moulding">Transitions and Mouldings</a>, <a href="https://drflooring.ca/collections/stair-nose">Stair noses</a>, <a href="https://drflooring.ca/collections/baseboard">Baseboards</a>, self-levelling, <a href="https://drflooring.ca/collections/underlay">Underlayment</a>, <a href="https://drflooring.ca/collections/glue-and-ahdesive">Glue and adhesives</a>, and more.\n\nSpend $2,000 or more on materials, installation or renovation and delivery is free to Langley, Surrey, Maple Ridge, Port Coquitlam and Coquitlam, BC. Spend $7,000 or more and delivery is free to Vancouver, Burnaby, Richmond, North Vancouver, West Vancouver, Squamish, Whistler, Abbotsford, Chilliwack and Mission.\n\nCall us about this product and our current offers — D&R Flooring and Renovations, https://www.drflooring.ca',
        label="Brand and offers block",
        help=(
            "Added to the end of every imported product description. This is "
            "where the offer lives, so it is a setting rather than code — a "
            "delivery threshold or a date written into the app would need a "
            "deploy to change, and would sit stale on the whole catalogue "
            "until someone noticed. Blank lines start new paragraphs, bare "
            "URLs become links, and <a href=\"...\"> works so the store's "
            "own categories can be linked — everything else is reduced to "
            "its text. Leave it empty to add nothing."
        ),
        group="Product import",
        kind="text",
        coerce=_as_optional_str,
        maximum=4000,
    ),
    Spec(
        key=IMPORT_TOP_BANNER,
        default="For SPECIAL prices, call us NOW at (604) 532-2211",
        label="Line at the top of every product",
        help=(
            "The first thing on every imported product page. Almost nothing "
            "in this catalogue is bought online — the conversion is a phone "
            "call — so this is the only line on the page asking for it. "
            "Bare URLs become links; no HTML. Empty adds nothing."
        ),
        group="Product import",
        kind="text",
        coerce=_as_optional_str,
        maximum=400,
    ),
    Spec(
        key=IMPORT_RELATED_LIMIT,
        default=60,
        label="Products cross-linked on each page",
        help=(
            "Every product in a range links to every other one, up to this "
            "many. The links are rendered into the description so they work "
            "on any theme and exist as HTML for a crawler, which is also why "
            "there is a limit at all: a 200-product range would put 199 "
            "thumbnails on every page and Shopify caps a description at "
            "65,535 characters. A flooring series is usually well under this."
        ),
        group="Product import",
        kind="int",
        coerce=_as_int,
        minimum=1,
        maximum=250,
    ),
    Spec(
        key=IMPORT_ALL_CHANNELS,
        default=True,
        label="Publish to every sales channel",
        help=(
            "Online Store, Shop, Google & YouTube, Facebook & Instagram, Buy "
            "Button — whatever the store has. A different switch from the "
            "status above: status decides whether a product is for sale at "
            "all, channels decide which ones carry it, and a product can be "
            "Active and still missing from the Online Store. Publishing a "
            "draft is safe, because it stays invisible until the status says "
            "otherwise. Without this it depends on each channel's own "
            "'automatically publish new products' setting."
        ),
        group="Product import",
        kind="bool",
        coerce=_as_bool,
    ),
    Spec(
        key=IMPORT_PUBLISH_STATUS,
        default="ACTIVE",
        label="Status new products are created with",
        help=(
            "ACTIVE puts each product on sale the moment it is created — "
            "including any mistake in the extraction, so a dry run first is "
            "worth the minutes. DRAFT keeps everything off the storefront "
            "until you set it active in Shopify admin. Separate from the "
            "channel setting above: a product has to be Active AND on a "
            "channel to be seen there."
        ),
        group="Product import",
        kind="str",
        coerce=_as_str,
    ),
    Spec(
        key=IMPORT_TAG_PREFIX,
        default="imported",
        label="Tag added to every imported product",
        help=(
            "How you find everything one import created, in Shopify admin "
            "and in a bulk edit, months later. Left blank, nothing marks an "
            "imported product as imported."
        ),
        group="Product import",
        kind="str",
        coerce=_as_str,
    ),
)

SPECS = SPECS + IMPORT_SPECS


def _schedule_specs() -> tuple[Spec, ...]:
    """An enabled-toggle and an hour for every scheduled job.

    Generated rather than written out four times, because the interesting
    thing about a job's schedule is the job, not the two identical settings
    it needs. `JobSpec.enabled_key` / `.hour_key` name these directly, so
    adding a job here and naming the keys there is the whole wiring.
    """
    jobs = (
        ("gsc_daily", "Search Console sync", 6),
        ("shopify_catalog", "Shopify catalogue snapshot", 5),
        ("ga4_daily", "Analytics sync", 6),
        ("ga4_city", "Analytics by city", 6),
        ("local_serp", "Local rank tracking", 5),
        ("ads_windsor", "Google Ads sync (Windsor)", 7),
        ("blog_articles", "Blog article index", 5),
        ("alerts", "Alert rules", 8),
        ("dataforseo_keywords", "Keyword market data", 4),
        ("competitor_catalog", "Competitor catalogue", 3),
        ("competitor_posts", "Competitor blog watch", 3),
        ("competitor_bestsellers", "Competitor best sellers", 2),
        ("competitor_matches", "Competitor match proposals", 4),
        ("publish_reconcile", "Reconcile published state", 5),
        ("product_import", "Continue product imports", 1),
        ("advisor_weekly", "Advisor notes", 9),
        ("strategy_weekly", "Strategy checkpoints", 10),
    )
    out: list[Spec] = []
    for name, label, default_hour in jobs:
        out.append(Spec(
            key=f"jobs.{name}.enabled", default=True,
            label=f"Run the {label} nightly",
            help="Turn off to leave the job manual-only. It stays runnable by hand.",
            group="Schedule", kind="bool", coerce=_as_bool,
        ))
        out.append(Spec(
            key=f"jobs.{name}.hour", default=default_hour,
            label=f"{label} — hour (local, 0-23)",
            help=(
                "Staggered by default so four jobs don't hit the proxy at once. "
                "Every source here lags at least a day, so the hour matters less "
                "than not colliding with the pipeline's own crons."
            ),
            group="Schedule", kind="int", coerce=_as_int, minimum=0, maximum=23,
        ))
    return tuple(out)


SPECS = SPECS + _schedule_specs()

_BY_KEY = {spec.key: spec for spec in SPECS}


def spec(key: str) -> Spec:
    try:
        return _BY_KEY[key]
    except KeyError:
        raise KeyError(f"unknown setting {key!r}") from None


def groups() -> dict[str, list[Spec]]:
    """Specs bucketed by group, in declaration order — the settings page's
    rendering order, so the form reads top to bottom as it was written."""
    out: dict[str, list[Spec]] = {}
    for s in SPECS:
        out.setdefault(s.group, []).append(s)
    return out


def get(key: str) -> Any:
    """The stored value, or the declared default when nothing is stored."""
    declared = spec(key)
    with get_session() as session:
        row = session.get(AppSetting, key)
        if row is None:
            return declared.default
        value = row.value
    if value is None:
        return declared.default
    try:
        return declared.clean(value)
    except (ValueError, TypeError):
        # A stored value that no longer validates (bounds tightened, say)
        # shouldn't break the job that reads it.
        return declared.default


def get_all() -> dict[str, Any]:
    return {s.key: get(s.key) for s in SPECS}


def set(key: str, raw: Any) -> Any:  # noqa: A001 - reads well as store.set()
    """Validate and persist one setting. Returns the cleaned value."""
    value = spec(key).clean(raw)
    with get_session() as session:
        row = session.get(AppSetting, key)
        if row is None:
            session.add(AppSetting(key=key, value_json=json.dumps(value)))
        else:
            row.value_json = json.dumps(value)
    return value


def set_many(items: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    """Validate every value before writing any of them.

    A settings form posts the whole page at once. Writing as it goes would
    leave half the form saved when the third field is bad, and the page would
    then redisplay a mix of old and new values with no way to tell which.
    """
    pending = {key: spec(key).clean(raw) for key, raw in items}
    with get_session() as session:
        for key, value in pending.items():
            row = session.get(AppSetting, key)
            if row is None:
                session.add(AppSetting(key=key, value_json=json.dumps(value)))
            else:
                row.value_json = json.dumps(value)
    return pending
