"""Central configuration loaded from environment / .env.

Every tunable in the PRD (model assignments, cadence, coverage target, gate
mode, confidence threshold) is surfaced here so the rest of the code never
reads os.environ directly. Optional integrations (image gen, SERP, Slack) are
detected via helper properties so stages can degrade gracefully when a key is
absent rather than crashing.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


# Fields where a blank value must NOT be read as "unset". Only database_url:
# its class default is SQLite, and blank in practice means a GitHub Actions
# `${{ secrets.DATABASE_URL }}` that was never populated — i.e. exactly the
# ephemeral-runner case where silently falling back to SQLite loses the
# calendar every run. db/session.py raises instead. Everywhere else the class
# default is the right answer for a blank value.
_BLANK_IS_MEANINGFUL = frozenset({"database_url"})


class _BlankAsUnset:
    """Treat a blank env var as absent so the field default applies.

    GitHub Actions expands an unset `${{ vars.X }}` / `${{ secrets.X }}` to an
    empty string rather than omitting the variable, so every optional setting
    arrives as "" and the class default never gets a chance. That reads fine
    for a str field but hard-fails anything typed — bool, int, float — with a
    parse error naming a field the user never set.

    Hooks prepare_field_value rather than filtering the source's output dict
    because that method is also where complex (list/dict) fields get JSON
    decoded: returning early keeps "" from ever reaching the decoder, which
    would otherwise raise SettingsError before any filtering could run.

    model_config's built-in `env_ignore_empty=True` covers most of this, but
    it applies to every field at once and _BLANK_IS_MEANINGFUL needs an out.
    """

    def prepare_field_value(
        self, field_name: str, field: FieldInfo, value: Any, value_is_complex: bool
    ) -> Any:
        if (
            isinstance(value, str)
            and not value.strip()
            and field_name not in _BLANK_IS_MEANINGFUL
        ):
            return None  # __call__ skips None, so the default survives
        return super().prepare_field_value(field_name, field, value, value_is_complex)


class _BlankAsUnsetEnvSource(_BlankAsUnset, EnvSettingsSource):
    pass


class _BlankAsUnsetDotEnvSource(_BlankAsUnset, DotEnvSettingsSource):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Same precedence as the default (init > env > .env > secrets file),
        with the two env sources swapped for blank-tolerant ones."""
        return (
            init_settings,
            _BlankAsUnsetEnvSource(settings_cls),
            _BlankAsUnsetDotEnvSource(settings_cls),
            file_secret_settings,
        )

    # ── LLM gateway (Google AI Studio, OpenAI-compatible endpoint) ──
    google_api_key: str = ""

    # Spread across distinct model names deliberately — AI Studio's free-tier
    # rate limits are per-model, so giving each stage its own model multiplies
    # the effective daily budget instead of every stage competing for one
    # model's 20-requests/day cap. Verified live against the actual model
    # catalog (GET /v1beta/models) — "gemini-3-flash" doesn't exist (the real
    # id is "-preview"), and "gemini-3.5-flash" / "gemini-flash-latest" were
    # both returning 503 (over capacity) as of 2026-07. Flip MODEL_DRAFT back
    # to gemini-3.5-flash once that settles down if you want the newer model.
    model_calendar: str = "gemini-3.1-flash-lite-preview"
    model_research: str = "gemini-3.1-flash-lite-preview"
    model_outline: str = "gemini-3.1-flash-lite"
    model_draft: str = "gemini-2.5-flash"
    model_seo: str = "gemini-3.1-flash-lite"
    model_qa: str = "gemini-3-flash-preview"

    # Models tried, in order, when a stage's primary model errors (transient
    # 429/503 over-capacity is common on the free tier). Reliable, generous-
    # quota models. The primary is always tried first regardless of this list.
    # Comma-separated string, not list[str]: pydantic-settings JSON-decodes
    # list-typed env vars, so list[str] would require '["a", "b"]' in .env and
    # in Actions vars rather than the plain a,b form used everywhere here.
    llm_fallback_models: str = "gemini-2.5-flash,gemini-3.1-flash-lite"

    # ── Linear (content calendar + draft handoff) ───────────────────
    linear_api_key: str = ""
    linear_team: str = ""
    linear_project: str = "Blog Content Calendar"

    # ── Shopify (auto-publish target for confident articles) ────────
    # When configured, articles that pass QA with confidence >= threshold
    # publish live to Shopify automatically; everything else waits in Linear.
    # enable_shopify_publish is a kill-switch independent of the credentials.
    enable_shopify_publish: bool = True
    # If false, confident articles are still created in Shopify but UNPUBLISHED
    # (hidden) for a human to review and click Publish in Shopify admin — a safe
    # staging mode. True = go straight live.
    shopify_publish_live: bool = True
    shopify_store_domain: str = ""
    shopify_access_token: str = ""
    shopify_blog_id: str = ""
    shopify_api_version: str = "2025-01"
    # Public storefront domain (e.g. drflooring.ca) — used for links inside
    # articles so they point at the live site, not the *.myshopify.com URL.
    # Falls back to shopify_store_domain when empty.
    public_domain: str = ""
    # Weave the store in as the place to buy: naturally position the shop in
    # the draft, and append a "Shop with us" CTA linking to the storefront.
    shop_promo: bool = True
    # Linear workflow states the pipeline moves an article's issue into.
    # Defaults match a stock Linear team (Backlog/Todo/In Progress/Done). If
    # your team has richer states, point these at e.g. "Ready to Review" /
    # "Needs Adjustments" / "Blocked". A name that doesn't exist on the team
    # falls back by state type so an issue is never left stuck in Backlog.
    linear_published_state: str = "Done"        # auto-published live to Shopify
    linear_review_state: str = "Todo"           # confident/ready — just needs a human to publish
    linear_needs_work_state: str = "Todo"       # low confidence / QA wants a look
    linear_blocked_state: str = "Todo"          # QA blocked (a comment explains why)

    # ── Optional integrations ────────────────────────────────────
    # Image generation: OpenRouter (fractions of a cent/image on Gemini's
    # token-based image billing) + Linear's own file storage for hosting —
    # no fal.ai account/billing needed. Master switch below so the stage can
    # be turned off deliberately even when a key is present (and vice versa).
    # Generative Engine Optimization: answer-first content, a visible FAQ
    # section, and JSON-LD (Article + FAQPage) structured data so AI answer
    # engines (ChatGPT/Claude/Gemini/AI Overviews) can parse and cite the page.
    enable_geo: bool = True

    enable_images: bool = False
    # When images aren't generated, drop each image slot's prompt into the body
    # as a bold [bracketed] placeholder so the user can generate the image
    # (e.g. with Shopify's AI) and swap it in before publishing.
    image_placeholders: bool = True
    openrouter_api_key: str = ""
    openrouter_image_model: str = "google/gemini-3.1-flash-lite-image"
    dataforseo_login: str = ""
    dataforseo_password: str = ""
    slack_webhook_url: str = ""

    # ── Google Search Console (real performance data) ────────────
    # The service-account key JSON, pasted whole as one env var. A path would
    # be friendlier locally but there's no filesystem to put it on in Actions,
    # and it's a credential either way — so it lives in a secret like the rest.
    gsc_credentials_json: str = ""
    # The property exactly as Search Console names it. Domain properties are
    # "sc-domain:drflooring.ca"; URL-prefix ones are "https://drflooring.ca/",
    # trailing slash included. Blank derives the domain form from
    # public_domain — see gsc_property. Wrong form is the usual first failure,
    # so `sync-performance` lists what the account can actually see.
    gsc_site_url: str = ""

    # ── WhatsApp (Meta Cloud API) — trigger the pipeline by message ──
    # access token: temporary 24h token (dev) or a permanent System User token.
    whatsapp_access_token: str = ""
    # Phone Number ID of your WhatsApp sender (WhatsApp > API Setup in the app).
    whatsapp_phone_number_id: str = ""
    # A string you invent; Meta echoes it back during webhook verification.
    whatsapp_verify_token: str = ""
    # App secret (App Settings > Basic) — used to verify webhook signatures.
    whatsapp_app_secret: str = ""
    # E.164 numbers allowed to trigger the pipeline (yours), comma-separated.
    # Others are ignored. Kept as a plain string so an empty value is valid.
    whatsapp_allowed_numbers: str = ""
    whatsapp_graph_version: str = "v21.0"

    # ── Tracing ──────────────────────────────────────────────────
    langsmith_api_key: str = ""
    langsmith_tracing: bool = False
    langsmith_project: str = "blog-pipeline"

    # ── Data store ───────────────────────────────────────────────
    database_url: str = "sqlite:///data/pipeline.db"

    # ── Pipeline behavior ────────────────────────────────────────
    # Articles with QA confidence below this land in "Needs Adjustments" in
    # Linear instead of "Ready to Review". Everything reaches Linear either way.
    confidence_threshold: float = 0.75
    cadence: str = "3x/week: Mon/Wed/Fri"
    coverage_target_weeks: int = 4
    word_count_target: int = 1500
    seo_min_score: int = 85

    brand_voice_path: str = "prompts/brand_voice.md"
    # Topics matching these (case-insensitive substring) are blocked by QA.
    # Comma-separated string — see llm_fallback_models above for why not list[str].
    banned_topics: str = ""

    # ── Topic research inputs (calendar agent) ───────────────────
    niche: str = ""
    seed_keywords: str = ""
    competitor_urls: str = ""
    # Bias research toward local + commercial/informational intent (good for a
    # local business like a flooring retailer/installer). Set business_location
    # to a city/region/service area to fold location into keywords + topics.
    local_seo: bool = True
    business_location: str = ""
    # The publishing business itself. Injected into draft + QA so first-party
    # brand mentions/CTAs are expected (not flagged as off-brand), and content
    # is written in the business's own voice.
    business_name: str = ""
    business_description: str = ""

    # ── Convenience flags ────────────────────────────────────────
    @property
    def store_link_base(self) -> str:
        """https://<public storefront domain> for in-article links (no trailing
        slash). Prefers public_domain, falls back to the myshopify domain."""
        domain = (self.public_domain or self.shopify_store_domain).strip()
        domain = domain.replace("https://", "").replace("http://", "").strip("/")
        return f"https://{domain}" if domain else ""

    @property
    def has_google(self) -> bool:
        return bool(self.google_api_key)

    @property
    def has_linear(self) -> bool:
        return bool(self.linear_api_key and self.linear_team)

    @property
    def has_shopify(self) -> bool:
        return bool(self.shopify_store_domain and self.shopify_access_token)

    @property
    def can_autopublish(self) -> bool:
        return self.enable_shopify_publish and self.has_shopify

    @property
    def has_images(self) -> bool:
        return self.enable_images and bool(self.openrouter_api_key)

    @property
    def has_dataforseo(self) -> bool:
        return bool(self.dataforseo_login and self.dataforseo_password)

    @property
    def gsc_property(self) -> str:
        """The Search Console property to query. Falls back to the domain form
        of public_domain, which is what "Add property -> Domain" produces."""
        if self.gsc_site_url:
            return self.gsc_site_url
        domain = self.public_domain.replace("https://", "").replace(
            "http://", ""
        ).strip("/")
        return f"sc-domain:{domain}" if domain else ""

    @property
    def has_search_console(self) -> bool:
        return bool(self.gsc_credentials_json and self.gsc_property)

    @property
    def has_slack(self) -> bool:
        return bool(self.slack_webhook_url)

    @property
    def has_whatsapp(self) -> bool:
        return bool(self.whatsapp_access_token and self.whatsapp_phone_number_id)

    @property
    def whatsapp_allowed_list(self) -> list[str]:
        return _csv(self.whatsapp_allowed_numbers)

    @property
    def llm_fallback_models_list(self) -> list[str]:
        return _csv(self.llm_fallback_models)

    @property
    def banned_topics_list(self) -> list[str]:
        return _csv(self.banned_topics)

    @property
    def seed_keywords_list(self) -> list[str]:
        return _csv(self.seed_keywords)

    @property
    def competitor_urls_list(self) -> list[str]:
        return _csv(self.competitor_urls)

    def enable_langsmith(self) -> None:
        """Wire LangSmith env vars so LangChain auto-traces every call."""
        import os

        if self.langsmith_tracing and self.langsmith_api_key:
            os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
            os.environ.setdefault("LANGCHAIN_API_KEY", self.langsmith_api_key)
            os.environ.setdefault("LANGCHAIN_PROJECT", self.langsmith_project)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.enable_langsmith()
    return settings
