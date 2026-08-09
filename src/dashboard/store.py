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

ADS_BACKFILL_DAYS = "ads.backfill_days"
ADS_RECENT_DAYS = "ads.recent_days"


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
        ("ads_windsor", "Google Ads sync (Windsor)", 7),
        ("blog_articles", "Blog article index", 5),
        ("alerts", "Alert rules", 8),
        ("dataforseo_keywords", "Keyword market data", 4),
        ("advisor_weekly", "Advisor notes", 9),
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
