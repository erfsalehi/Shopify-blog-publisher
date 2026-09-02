"""Dashboard settings — the machine-level ones only.

Deliberately thin. Anything the owner might want to change while the app is
running (competitor sites, watchlists, alert thresholds, job times) lives in
the `app_setting` table instead, because editing `.env` and restarting is not
a settings page. This file holds only what must be known *before* the app can
start: where to listen and which database to open.

Credentials are not duplicated here. `blog_pipeline.config.get_settings()` is
the single source for those, and this module re-exports it as `pipeline()` so
dashboard code has one obvious way to reach `GSC_CREDENTIALS_JSON` and friends.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from blog_pipeline.config import Settings as PipelineSettings
from blog_pipeline.config import get_settings as pipeline


def is_serverless() -> bool:
    """Running as a Vercel function rather than a long-lived process.

    Not a setting — it's a fact about the runtime, set by the platform, and
    nothing should be able to override it from `.env`. Two things depend on
    it, and both are cases where a background thread or timer is silently
    useless rather than merely slower: the scheduler
    (`enable_scheduler_effective`) and "Run now" (web.py), because the
    function is frozen the instant its response is sent.
    """
    return os.environ.get("VERCEL") == "1"


#: Where each host publishes the commit it built, in the order they're asked.
#: `GIT_COMMIT_SHA` is last and is ours: a plain override for a host that
#: publishes nothing, or for running this from a checkout.
_COMMIT_VARS = (
    "VERCEL_GIT_COMMIT_SHA",
    "RAILWAY_GIT_COMMIT_SHA",
    "RENDER_GIT_COMMIT",
    "HEROKU_SLUG_COMMIT",
    "SOURCE_VERSION",
    "GIT_COMMIT_SHA",
)


def build_commit() -> str:
    """The short SHA this deployment was built from, or "" if nothing says.

    Deliberately here rather than in a settings class: like `is_serverless`,
    it is a fact the platform states about the running process, not something
    anyone should be able to set in `.env` to a value that isn't true.

    It exists because "is the fix live?" was, for three consecutive imports,
    unanswerable from the app. A run reproduced a bug that had already been
    fixed, and the only way to tell that the fix had not been deployed was to
    read the log's wording closely enough to notice it came from the old
    code. A footer that names the commit answers it in a glance, and an
    answer nobody has to reason for is the point.
    """
    for name in _COMMIT_VARS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value[:7]
    return ""


def build_branch() -> str:
    """The branch this deployment was built from, or "" if nothing says.

    The other half of the same question. A deployment can be perfectly
    up to date with `main` and still not have the change, because the change
    is on a branch — which is exactly what happened.
    """
    for name in (
        "VERCEL_GIT_COMMIT_REF", "RAILWAY_GIT_BRANCH", "RENDER_GIT_BRANCH",
        "GIT_BRANCH",
    ):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value[:60]
    return ""


class DashboardSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="DASHBOARD_",
        extra="ignore",
        case_sensitive=False,
    )

    # Loopback by default for `python -m dashboard`, which refuses any other
    # value (see __main__.py) — the original design had no auth because the
    # app simply wasn't reachable from anywhere else. On Vercel this setting
    # is irrelevant (the platform terminates the connection, not uvicorn), and
    # `password` below is what actually gates access once the app is public.
    host: str = "127.0.0.1"
    port: int = 8600

    # Its own file rather than the pipeline's data/pipeline.db. The pipeline's
    # schema is managed by its own init-db, and sharing one database would
    # force the dashboard's schema to migrate in lockstep with a database it
    # doesn't own. Point this at Postgres (Neon, Supabase, ...) for Vercel —
    # a serverless function has no persistent disk to put a SQLite file on.
    #
    # Falls back to the bare DATABASE_URL / POSTGRES_URL names before the
    # sqlite default, not because the DASHBOARD_ prefix is wrong but because
    # connecting Neon through Vercel's own Storage tab auto-populates
    # DATABASE_URL — the integration has no idea this app expects
    # DASHBOARD_DATABASE_URL, and a click-to-connect that silently keeps
    # pointing at a SQLite path with no disk under it is a deploy that looks
    # fine until the first write. DASHBOARD_DATABASE_URL still wins if both
    # are set, so an explicit override is never shadowed by the integration.
    database_url: str = Field(
        "sqlite:///data/dashboard.db",
        validation_alias=AliasChoices(
            "DASHBOARD_DATABASE_URL", "DATABASE_URL", "POSTGRES_URL",
        ),
    )

    # Run scheduled jobs in-process. Off gives a read-only viewer of whatever
    # is already in the DB — useful when debugging the UI against the proxy,
    # and **required** on Vercel: no process there lives long enough to hold
    # an APScheduler timer, so jobs run as Vercel Cron hitting /api/cron/*
    # instead (see cron.py). Leaving this on under Vercel wouldn't error, it
    # would just start a scheduler that gets torn down with the function and
    # never fires anything — silent, not safe.
    #
    # Not relied on as the only guard: see `enable_scheduler_effective` below,
    # which forces this off under Vercel regardless of what's configured, so
    # a forgotten env var can't produce the silent-failure case above.
    enable_scheduler: bool = True

    # Same trailing-newline hazard as the secrets below (`echo "$v" | vercel
    # env add` appends `\n`), but pydantic's bool parser doesn't strip
    # whitespace before matching against "true"/"false" — it raises instead
    # of falling back, which took the whole app down at import time rather
    # than just misreading a flag. Caught for real on the first Vercel deploy.
    @field_validator("enable_scheduler", mode="before")
    @classmethod
    def _strip_bool(cls, value):
        if isinstance(value, str):
            return value.strip()
        return value

    @property
    def enable_scheduler_effective(self) -> bool:
        if is_serverless():
            return False
        return self.enable_scheduler

    # Auto-reload templates/code. Off in normal use.
    reload: bool = False

    # ── Access control ───────────────────────────────────────────────
    # Empty (the local default) means no login is required — unchanged
    # behaviour for `python -m dashboard` on loopback, where reachability was
    # always the real access control. Set once this is public on Vercel: one
    # shared password for the one owner, not a users table for one person.
    password: str = ""
    # Signs the session cookie. Required whenever `password` is set — an
    # unset or rotating secret would silently log everyone out (or, worse,
    # accept a session signed by a previous random secret if one were
    # generated per process). No default is provided on purpose: a guessed or
    # baked-in default defeats the signature the moment two deployments share
    # it. Generate one with `python -c "import secrets; print(secrets.token_hex(32))"`.
    session_secret: str = ""
    # How long a login lasts before the cookie itself expires, independent of
    # activity. A dashboard checked a few times a week wants this measured in
    # weeks, not the short-lived sessions a banking app would use.
    session_max_age_days: int = 30

    @property
    def auth_required(self) -> bool:
        return bool(self.password.strip())

    # Separate from the owner's login on purpose. Vercel Cron calls
    # /api/cron/* with no browser and no session cookie to send — it
    # authenticates by sending `Authorization: Bearer <CRON_SECRET>`
    # automatically whenever a CRON_SECRET env var exists on the project, per
    # Vercel's own convention (not DASHBOARD_-prefixed, so Vercel's UI and
    # this app agree on the name without translation). A cron endpoint
    # guarded by the owner's password would have no way to receive it.
    cron_secret: str = Field("", validation_alias="CRON_SECRET")

    @property
    def cron_configured(self) -> bool:
        return bool(self.cron_secret.strip())

    # Stripped at the settings boundary, not just at each call site — found
    # the hard way. `vercel env add` reads its value from stdin, and piping
    # via `echo "$value" |` appends a trailing newline that becomes part of
    # the stored secret. Vercel's own build refuses to deploy a CRON_SECRET
    # with a trailing newline (it can't sit in an HTTP header), which is a
    # loud failure — but the same trailing whitespace on DASHBOARD_PASSWORD
    # or DASHBOARD_SESSION_SECRET would just silently reject every login
    # attempt against an apparently-correct password, which is a much worse
    # failure to have to diagnose. Stripping here means it can't happen
    # again regardless of which of the fields it lands on, or whether it was
    # `echo` piping, a copy-paste into Vercel's UI, or anything else.
    #
    # The API keys are here for the same reason and one more: both go out as
    # `Authorization: Bearer <key>`, and a newline inside a header value is
    # rejected by httpx before the request is ever sent — so the failure
    # arrives as a protocol error naming neither the key nor the newline.
    @field_validator(
        "password", "session_secret", "cron_secret",
        "windsor_api_key", "firecrawl_api_key",
        mode="after",
    )
    @classmethod
    def _strip_secret(cls, value: str) -> str:
        return value.strip()

    # ── Dashboard-only credentials ──────────────────────────────────
    # Windsor.ai aggregates Google Ads (and much else) behind one REST API.
    # `validation_alias` overrides the DASHBOARD_ prefix so this reads the
    # plain WINDSOR_API_KEY, which is what Windsor's own docs call it —
    # a credential renamed on the way into .env is a credential someone
    # eventually pastes into the wrong slot.
    #
    # Note this is NOT the Windsor MCP connector: that one is a tool inside
    # Claude's session and is unreachable from a scheduled job running on
    # this machine. Same account, different door. Key from
    # https://onboard.windsor.ai/app/data-preview
    windsor_api_key: str = Field("", validation_alias="WINDSOR_API_KEY")

    @property
    def has_windsor(self) -> bool:
        return bool(self.windsor_api_key.strip())

    # Renders a collection page with a real browser before handing back its
    # HTML — the product importer's fallback for a manufacturer whose grid is
    # filled in by JavaScript, which a plain GET can never see. Key from
    # https://www.firecrawl.dev/app/api-keys. Optional: the importer's plain
    # fetch is tried first regardless, and this only fires when that comes
    # back with nothing to parse.
    firecrawl_api_key: str = Field("", validation_alias="FIRECRAWL_API_KEY")

    @property
    def has_firecrawl(self) -> bool:
        return bool(self.firecrawl_api_key.strip())


@lru_cache
def get_settings() -> DashboardSettings:
    return DashboardSettings()


__all__ = ["DashboardSettings", "get_settings", "pipeline", "PipelineSettings"]
