"""Dashboard tables.

Every external pull lands here with a `fetched_at`, and the UI reads nothing
else. Three shapes recur and are worth naming once:

  * **Settings** (`AppSetting`) — owner-editable config. In the database, not
    `.env`, so a settings page can write it without a restart.
  * **Job log** (`JobRun`) — one row per attempt at a sync, successful or not.
    Given a proxy that fails intermittently, "did last night's sync work?" is
    a question the app must be able to answer out loud.
  * **Metric snapshots** (`GscSiteDaily`, `GscPageDaily`, ...) — one row per
    day per dimension, upserted. Daily granularity is the point: the
    pipeline's own `search_performance` table stores 90-day windows, which
    can tell you a post is decaying but cannot draw a line.

Statuses are stored as plain strings rather than SQL enums on purpose. The
pipeline learned the hard way that `create_all()` builds an enum type once and
never alters it, so a value added later is missing forever (see
`blog_pipeline.db.session._sync_enum_labels`). A `String` column with a Python
`StrEnum` in front of it gives the same type safety in code with no schema to
drift.
"""

from __future__ import annotations

import enum
import json
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Separate from `blog_pipeline.db.models.Base` — different database."""


class JobStatus(str, enum.Enum):
    running = "running"
    ok = "ok"
    error = "error"
    # Ran, did nothing, and that's correct — e.g. Search Console isn't
    # configured. Distinct from `error` so the jobs page can stay green.
    skipped = "skipped"


class AppSetting(Base):
    """Owner-editable config, one row per key, value stored as JSON text.

    JSON rather than a typed column per setting because the shapes are all
    over the place — a bool for "email alerts on", a list of dicts for the
    competitor list — and the alternative is a schema migration every time the
    owner needs one more knob.
    """

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, nullable=False, default="null")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )

    @property
    def value(self) -> Any:
        try:
            return json.loads(self.value_json)
        except ValueError:
            # A hand-edited row shouldn't take the settings page down with it.
            return None


class JobRun(Base):
    """One attempt at one sync job.

    Written twice: once as `running` the moment the job starts, then updated
    on completion. Starting the row up front is what makes a job that hard-
    crashes the process visible afterwards — a row that says `running` with an
    old `started_at` is the evidence. A row only written on success would just
    be absent, which looks identical to "never scheduled".
    """

    __tablename__ = "job_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default=JobStatus.running.value, nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, index=True, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # How many rows the job wrote. The one number that answers "did it
    # actually do anything" without opening the detail blob.
    rows: Mapped[int] = mapped_column(Integer, default=0)
    # Transient-failure retries burned before the outcome below. Non-zero on a
    # successful run is the proxy misbehaving and worth seeing.
    attempts: Mapped[int] = mapped_column(Integer, default=1)

    # Whatever the job wants to report, JSON. Rendered as-is on the jobs page.
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Who started it — "scheduler" or "manual". A manual run failing while the
    # nightly one succeeds usually means the owner clicked during a proxy dip.
    trigger: Mapped[str] = mapped_column(String(20), default="scheduler")

    @property
    def detail(self) -> dict:
        try:
            loaded = json.loads(self.detail_json)
        except ValueError:
            return {}
        return loaded if isinstance(loaded, dict) else {}


class GscSiteDaily(Base):
    """Search Console site totals, one row per day.

    `date` is the primary key: Google restates the last few days as its data
    settles, so every sync re-pulls a trailing window and overwrites rather
    than appends. Appending would double-count exactly the days that matter
    most.
    """

    __tablename__ = "gsc_site_daily"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    ctr: Mapped[float] = mapped_column(Float, default=0.0)
    position: Mapped[float] = mapped_column(Float, default=0.0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class GscPageDaily(Base):
    """Search Console per-URL rows, one per day per page.

    The join key for everything downstream — a product's search performance, a
    blog post's decay curve — so the URL is stored exactly as Google reports
    it (canonical, absolute) and normalised at read time by the callers that
    need to match it against our own stored URLs.
    """

    __tablename__ = "gsc_page_daily"
    __table_args__ = (UniqueConstraint("date", "page", name="uq_gsc_page_daily"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    page: Mapped[str] = mapped_column(String(1000), index=True, nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    ctr: Mapped[float] = mapped_column(Float, default=0.0)
    position: Mapped[float] = mapped_column(Float, default=0.0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class GscFetchDay(Base):
    """A day we have successfully fetched from Search Console, per dimension.

    Needed because "we have rows for that day" is not the same claim as "we
    asked about that day". A day with genuinely no impressions returns no
    rows, and inferring coverage from the data would leave the sync
    re-fetching every quiet day for the life of the install — a slow leak
    that only shows up as unexplained API calls months later.
    """

    __tablename__ = "gsc_fetch_day"
    __table_args__ = (UniqueConstraint("date", "kind", name="uq_gsc_fetch_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # site | page
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class GscQueryDaily(Base):
    """Search Console per-query rows, one per day per search term.

    What the site is *shown for*, as opposed to `GscPageDaily`'s what-gets-
    clicked. This is the half that makes striking-distance work: a term
    earning impressions from position 8-30 is one Google already considers
    the site relevant to, which is a far shorter path to page one than a
    high-volume term with no history.
    """

    __tablename__ = "gsc_query_daily"
    __table_args__ = (UniqueConstraint("date", "query", name="uq_gsc_query_daily"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    query: Mapped[str] = mapped_column(String(500), index=True, nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    ctr: Mapped[float] = mapped_column(Float, default=0.0)
    position: Mapped[float] = mapped_column(Float, default=0.0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class KeywordMetric(Base):
    """Market data for a search term, from DataForSEO.

    Search Console says how the site does on a term. This says how big the
    term is and what a click on it is worth — the two things Search Console
    structurally cannot know. Together they rank opportunity: high volume,
    real commercial value, and already ranking 8-30.

    Keyed by (keyword, location_code) because volume is market-specific, and
    getting that wrong is not a rounding error: the pipeline had been asking
    for 2840 (United States) while the business serves Langley, British
    Columbia.
    """

    __tablename__ = "keyword_metric"
    __table_args__ = (
        UniqueConstraint("keyword", "location_code", name="uq_keyword_metric"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    keyword: Mapped[str] = mapped_column(String(500), index=True, nullable=False)
    location_code: Mapped[int] = mapped_column(Integer, index=True, nullable=False)

    # Null means DataForSEO answered but has no volume for this term — a real
    # answer, and different from "we never asked", which is a missing row.
    search_volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    competition: Mapped[float | None] = mapped_column(Float, nullable=True)
    cpc: Mapped[float | None] = mapped_column(Float, nullable=True)

    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class ApiSpend(Base):
    """One paid API call, with what it cost.

    DataForSEO bills per request against a prepaid balance that was $1.00 when
    this was written — about eleven requests. A dashboard that can quietly
    spend money needs its own ledger, not a number in someone else's console.
    """

    __tablename__ = "api_spend"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), index=True, default="dataforseo")
    endpoint: Mapped[str] = mapped_column(String(120), nullable=False)
    requests: Mapped[int] = mapped_column(Integer, default=1)
    # What we believe it cost. DataForSEO also returns an authoritative cost
    # per response; when present that's what's stored.
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    items: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class AdvisorNote(Base):
    """One generated read of how a section of the business is doing.

    Stored rather than generated per page load: the free Gemini tier is
    rate-limited, and advice that changes every refresh is advice nobody
    trusts. Each note records the exact context it was given, so a suggestion
    can always be traced back to the numbers behind it.
    """

    __tablename__ = "advisor_note"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # overview | products | blog | keywords | ads | experiments
    scope: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)

    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The brief the model was given, verbatim. Without it a note is an
    # assertion; with it, it's checkable.
    context_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Figures in the output that could not be found in the brief. Empty is
    # the expected case; anything here is shown as a warning rather than
    # silently trusted.
    unverified_json: Mapped[str] = mapped_column(Text, default="[]")

    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    @property
    def unverified(self) -> list[str]:
        try:
            loaded = json.loads(self.unverified_json)
        except ValueError:
            return []
        return loaded if isinstance(loaded, list) else []


class AdvisorAction(Base):
    """A single suggestion, tracked to an outcome.

    This is what makes the advisor's "memory" mean something. Feeding old
    notes back in only tells the model what it once said; feeding back which
    suggestions were *done* and which were *dismissed* lets it say "you
    changed those titles three weeks ago — here is what CTR did since", and
    stop repeating advice that was already rejected.
    """

    __tablename__ = "advisor_action"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    note_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    scope: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # open | done | dismissed
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ShopifyProduct(Base):
    """A product as it existed at the last catalogue sync.

    `first_seen` and `last_seen` rather than delete-and-reinsert: a product
    that disappears from the catalogue is interesting — it was delisted, or
    the sync only got half the pages — and a row that silently vanishes can
    tell you neither. Anything whose `last_seen` is older than the newest sync
    is presented as gone rather than dropped.

    Prices are stored even though ~94% of this catalogue sits at 0.00 with
    prices hidden by the Orichi app. The zero is the fact: it's what marks a
    product as "call for price", and it's what the show-price rollout will
    change.
    """

    __tablename__ = "shopify_product"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The Admin API global id, e.g. "gid://shopify/Product/123". Stable across
    # handle and title changes, which is why it's the key rather than handle.
    product_gid: Mapped[str] = mapped_column(
        String(120), unique=True, index=True, nullable=False
    )
    handle: Mapped[str] = mapped_column(String(300), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(600), nullable=False)
    product_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    vendor: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)

    price_min: Mapped[float] = mapped_column(Float, default=0.0)
    price_max: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    total_inventory: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # JSON list. Tags drive the show-price mechanism and the experiment
    # cohorts, so they're queried, not decorative.
    tags_json: Mapped[str] = mapped_column(Text, default="[]")

    # The public storefront URL, built once at sync time so every reader joins
    # against the same string rather than re-deriving it slightly differently.
    online_url: Mapped[str | None] = mapped_column(String(700), index=True)

    first_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    @property
    def tags(self) -> list[str]:
        try:
            loaded = json.loads(self.tags_json)
        except ValueError:
            return []
        return loaded if isinstance(loaded, list) else []

    @property
    def has_visible_price(self) -> bool:
        """True when the storefront shows a number instead of 'Call for price'."""
        return self.price_min > 0


class Ga4Daily(Base):
    """GA4 site totals, one row per day."""

    __tablename__ = "ga4_daily"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    sessions: Mapped[int] = mapped_column(Integer, default=0)
    users: Mapped[int] = mapped_column(Integer, default=0)
    engaged_sessions: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Ga4EventDaily(Base):
    """One GA4 event's count for one day.

    This is where the store's actual conversions live. Nothing is bought on
    this site — 94% of the catalogue hides its price behind "Call for price"
    — so `call_click`, `call_for_price_click` and `whatsapp_click` are the
    outcome. Sessions are traffic; these are business.
    """

    __tablename__ = "ga4_event_daily"
    __table_args__ = (UniqueConstraint("date", "event_name", name="uq_ga4_event_daily"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    event_name: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class AdsCampaignDaily(Base):
    """Google Ads campaign performance for one day.

    `source` records which pipe the row came in through — Windsor today, the
    direct Google Ads API once the developer token clears. Both will exist at
    once during the changeover, and a spend figure whose origin is ambiguous
    is a spend figure nobody trusts.
    """

    __tablename__ = "ads_campaign_daily"
    __table_args__ = (
        UniqueConstraint("date", "campaign", "source", name="uq_ads_campaign_daily"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    campaign: Mapped[str] = mapped_column(String(400), index=True, nullable=False)
    campaign_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    campaign_status: Mapped[str | None] = mapped_column(String(40), nullable=True)

    spend: Mapped[float] = mapped_column(Float, default=0.0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    # Fractional on purpose: Google attributes partial conversions, and this
    # account's data really does contain values like 1.5 and 2.5. Rounding at
    # storage time would quietly change the numbers.
    conversions: Mapped[float] = mapped_column(Float, default=0.0)

    source: Mapped[str] = mapped_column(String(30), default="windsor", nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class BlogArticle(Base):
    """A blog post, copied from the pipeline's own database.

    Copied rather than read across, for two reasons. The page needs to join
    articles against this database's daily Search Console rows, and a
    cross-database join has to happen in Python either way. And the pipeline's
    database is SQLite locally but Postgres in CI — a dashboard that reached
    into it directly would be coupled to a schema and a connection string it
    doesn't own.

    Nothing here is authoritative: `blog_articles` re-copies it every run, and
    the pipeline remains the only thing that writes an article.
    """

    __tablename__ = "blog_article"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Primary key in the pipeline's `article` table.
    pipeline_id: Mapped[int] = mapped_column(
        Integer, unique=True, index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(600), nullable=False)
    topic: Mapped[str | None] = mapped_column(String(600), nullable=True)
    status: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    shopify_url: Mapped[str | None] = mapped_column(String(700), index=True)
    shopify_article_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    linear_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    linear_identifier: Mapped[str | None] = mapped_column(String(40), nullable=True)

    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # From the pipeline's `article_revision` table: refreshes snapshot the old
    # body before overwriting, so the newest revision dates the last refresh.
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revision_count: Mapped[int] = mapped_column(Integer, default=0)

    seo_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    qa_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    synced_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class RefreshProposal(Base):
    """A dry-run refresh, stored so a human can approve the exact bytes.

    The point of a preview is that what you approved is what ships. Re-running
    the refresh on approval would call the model again and publish something
    the owner never saw — the summary would still say "shortened the intro",
    and the article would be different. So the proposed HTML is stored here
    and that stored HTML is what gets written.

    `original_sha` fingerprints the live body the proposal was generated
    against. If the live article changed in the meantime — someone edited it
    in Shopify admin, or the weekly cron refreshed it — applying this proposal
    would silently revert their work, so the apply path refuses and asks for a
    fresh preview.
    """

    __tablename__ = "refresh_proposal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The pipeline's article id — the same key run_refresh(only_ids=...) takes.
    article_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    # pending | applied | failed | stale | skipped
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)

    original_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    changes_json: Mapped[str] = mapped_column(Text, default="[]")
    image_suggestions_json: Mapped[str] = mapped_column(Text, default="[]")

    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def changes(self) -> list[str]:
        try:
            loaded = json.loads(self.changes_json)
        except ValueError:
            return []
        return loaded if isinstance(loaded, list) else []

    @property
    def image_suggestions(self) -> list[dict]:
        try:
            loaded = json.loads(self.image_suggestions_json)
        except ValueError:
            return []
        return loaded if isinstance(loaded, list) else []


class AlertRule(Base):
    """A threshold the owner wants to be told about.

    Rules are rows rather than code so a threshold can be tuned at 7am without
    an edit-and-restart. `kind` selects the evaluator; `threshold` means
    whatever that evaluator says it means, which the settings UI spells out
    per kind rather than leaving as a bare number.
    """

    __tablename__ = "alert_rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    threshold: Mapped[float] = mapped_column(Float, default=0.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Send to Slack as well as the inbox. Off by default: an alert nobody
    # asked to be interrupted by is how alerting gets muted entirely.
    notify: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Alert(Base):
    """One firing of one rule.

    `fingerprint` is what keeps this from becoming noise: it identifies the
    specific condition (rule + subject + the day it was evaluated for), and an
    open alert with the same fingerprint is updated rather than duplicated. A
    rule that fires every night for three weeks should be one row you haven't
    dealt with yet, not twenty-one.
    """

    __tablename__ = "alert"
    __table_args__ = (UniqueConstraint("fingerprint", name="uq_alert_fingerprint"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    kind: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(400), nullable=False)

    title: Mapped[str] = mapped_column(String(400), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Where to go to do something about it.
    link: Mapped[str | None] = mapped_column(String(700), nullable=True)
    severity: Mapped[str] = mapped_column(String(20), default="warn")

    first_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    times_seen: Mapped[int] = mapped_column(Integer, default=1)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Set when an evaluation ran and the condition was no longer present. This
    # is what lets "acknowledged" mean "hide until it actually changes" rather
    # than "hide until tomorrow" — without it, an acknowledged alert either
    # reopens every night (useless) or never reopens (dangerous).
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def open(self) -> bool:
        return self.acknowledged_at is None and self.resolved_at is None


class Experiment(Base):
    """A cohort + time based test.

    Per-visitor split testing is not possible in this stack and the design
    says so rather than pretending: search shows one title to everyone, and
    Shopify cannot show different prices to different visitors. What *is*
    possible — and is what the industry does for SEO — is a treatment group
    against a matched control group over the same dates, scored by
    difference-in-differences.

    `variable` records what is being changed, because the metric that answers
    it differs: CTR for a title test, calls and impressions for a price test.
    """

    __tablename__ = "experiment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    hypothesis: Mapped[str | None] = mapped_column(Text, nullable=True)
    # title | description | price | strategy
    variable: Mapped[str] = mapped_column(String(40), nullable=False)
    # draft (membership still editable) | running (baseline frozen) | ended
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)

    started_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    ends_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Days of history frozen as the baseline when the experiment starts.
    baseline_days: Mapped[int] = mapped_column(Integer, default=28)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class ExperimentProduct(Base):
    """One product's membership of one experiment, with its frozen baseline.

    Membership lives here rather than in a Shopify tag on purpose. A tag is
    public surface: it can leak into a storefront filter, and anyone editing
    the product in admin can remove it without knowing what it meant. It also
    has to stay fixed for the life of the test — a treatment product that
    quietly leaves the group invalidates the result rather than changing it.

    The baseline columns are a *snapshot*, written once when the experiment
    starts. Recomputing "before" from live data later would let the comparison
    move under the result, which is the classic way a test proves whatever you
    hoped it would.
    """

    __tablename__ = "experiment_product"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id", "product_gid", name="uq_experiment_product"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    experiment_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    product_gid: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    # treatment | control
    cohort: Mapped[str] = mapped_column(String(20), index=True, nullable=False)

    baseline_clicks: Mapped[int] = mapped_column(Integer, default=0)
    baseline_impressions: Mapped[int] = mapped_column(Integer, default=0)
    baseline_position_weight: Mapped[float] = mapped_column(Float, default=0.0)
    baseline_frozen_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )

    # What the treatment actually changed, kept so a result can be explained
    # and so the change can be reverted by hand.
    before_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    apply_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class CompetitorPlatform(str, enum.Enum):
    """How a competitor's site gives up its data.

    Worth storing rather than re-probing: it decides which collector runs,
    and it changes about never. `unknown` means the probe hasn't run;
    `other` means it ran and found no machine-readable catalogue, which is a
    different thing and shouldn't be retried every night as though it were
    the first attempt.
    """

    unknown = "unknown"
    shopify = "shopify"
    other = "other"


class Competitor(Base):
    """A competitor site the owner added in the app.

    The owner supplies name and base URL; everything else is discovered. Most
    local flooring retailers run Shopify, which publishes its whole catalogue
    at /products.json and its blog as Atom — no scraping, no parsing HTML
    that changes next week. `platform` records which door was open.
    """

    __tablename__ = "competitor"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    # Set when a site resists JSON-LD and OpenGraph extraction and the owner
    # has to point at the price element by hand. Null means try the standard
    # extraction order.
    price_selector: Mapped[str | None] = mapped_column(String(300), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    platform: Mapped[str] = mapped_column(
        String(20), default=CompetitorPlatform.unknown.value
    )
    # Why the last collection attempt found nothing, if it found nothing.
    # Kept on the competitor rather than only in the job log so the page can
    # say "this one is not readable" next to the competitor it's about.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def host(self) -> str:
        """`drflooring.ca` — for display, and for building absolute URLs."""
        raw = (self.base_url or "").strip().rstrip("/")
        for prefix in ("https://", "http://"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
        return raw.split("/")[0]


class CompetitorProduct(Base):
    """One product in a competitor's catalogue, as of the last collection.

    `first_seen` / `last_seen` rather than delete-and-reinsert, for the same
    reason as `ShopifyProduct`: a product leaving a catalogue is a fact worth
    keeping, and it's indistinguishable from a half-finished sync if the row
    just disappears.

    Price lives here as the *current* price and in `CompetitorProductPrice`
    as history. Duplicated deliberately — every list view wants today's
    number, and joining to a history table for it would make the common read
    the expensive one.
    """

    __tablename__ = "competitor_product"
    __table_args__ = (
        UniqueConstraint("competitor_id", "handle", name="uq_competitor_handle"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    competitor_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)

    # Their own identifier where one exists (Shopify's numeric product id),
    # else the URL path. Only unique within one competitor, hence the
    # composite constraint above.
    handle: Mapped[str] = mapped_column(String(400), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    title: Mapped[str] = mapped_column(String(600), nullable=False)
    vendor: Mapped[str | None] = mapped_column(String(200), index=True, nullable=True)
    product_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    url: Mapped[str | None] = mapped_column(String(800), nullable=True)

    price_min: Mapped[float] = mapped_column(Float, default=0.0)
    price_max: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    available: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Their publish date, when the feed gives one. A competitor's recent
    # additions say what they're betting on this season.
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Position in their best-selling collection, 1 = best seller. Null until
    # the best-seller job has run. This is the closest thing to their sales
    # data that exists publicly.
    best_seller_rank: Mapped[int | None] = mapped_column(Integer, index=True)
    best_seller_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    first_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class CompetitorProductPrice(Base):
    """A competitor product's price on one day.

    One row per product per day, upserted — the same shape as every other
    snapshot table here, and for the same reason: "are they discounting?" is
    a question about a line, not a number.
    """

    __tablename__ = "competitor_product_price"
    __table_args__ = (
        UniqueConstraint(
            "competitor_product_id", "date", name="uq_competitor_price_day"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    competitor_product_id: Mapped[int] = mapped_column(
        Integer, index=True, nullable=False
    )
    date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    price_min: Mapped[float] = mapped_column(Float, default=0.0)
    price_max: Mapped[float] = mapped_column(Float, default=0.0)
    available: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    best_seller_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CompetitorPost(Base):
    """A blog post on a competitor's site.

    The point isn't to read them, it's to see the shape: how often they
    publish, and about what. A competitor posting weekly about "laminate vs
    vinyl in Langley" is telling you which searches they intend to own.
    """

    __tablename__ = "competitor_post"
    __table_args__ = (
        UniqueConstraint("competitor_id", "url", name="uq_competitor_post_url"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    competitor_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)

    url: Mapped[str] = mapped_column(String(800), nullable=False)
    title: Mapped[str] = mapped_column(String(600), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(String(200), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime, index=True, nullable=True
    )

    # Set once the owner has queued a reply article, so the "Counter this"
    # button can't be pressed twice and the page can show what's already
    # answered.
    countered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    countered_topic: Mapped[str | None] = mapped_column(String(500), nullable=True)

    first_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class MatchStatus(str, enum.Enum):
    proposed = "proposed"
    confirmed = "confirmed"
    rejected = "rejected"


class CompetitorMatch(Base):
    """"Their product X is our product Y" — proposed by heuristics, decided
    by the owner.

    Matching flooring SKUs across brands is genuinely hard: the same board is
    sold as different series names by different retailers, and the only
    reliable discriminators (thickness, wear layer, AC rating) are buried in
    free text. So the machine proposes and scores, and the owner confirms
    once. A confirmed match persists and is never re-proposed.

    `rejected` is kept rather than deleted for exactly that reason — a
    deleted rejection is a proposal the next run makes again.
    """

    __tablename__ = "competitor_match"
    __table_args__ = (
        UniqueConstraint(
            "competitor_product_id", "shopify_product_id", name="uq_competitor_match"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    competitor_product_id: Mapped[int] = mapped_column(
        Integer, index=True, nullable=False
    )
    shopify_product_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)

    status: Mapped[str] = mapped_column(
        String(20), default=MatchStatus.proposed.value, index=True
    )
    # 0..1. Not a probability — a heuristic sum, useful only for ordering the
    # review queue so the obvious matches are confirmed first.
    score: Mapped[float] = mapped_column(Float, default=0.0)
    # Which signals fired, so the review queue can show its working rather
    # than asking the owner to trust a number.
    reason: Mapped[str | None] = mapped_column(String(400), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
