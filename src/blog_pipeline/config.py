"""Central configuration loaded from environment / .env.

Every tunable in the PRD (model assignments, cadence, coverage target, gate
mode, confidence threshold) is surfaced here so the rest of the code never
reads os.environ directly. Optional integrations (image gen, SERP, Slack) are
detected via helper properties so stages can degrade gracefully when a key is
absent rather than crashing.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── LLM gateway ──────────────────────────────────────────────
    openrouter_api_key: str = ""

    model_calendar: str = "anthropic/claude-haiku-4.5"
    model_research: str = "anthropic/claude-haiku-4.5"
    model_outline: str = "anthropic/claude-haiku-4.5"
    model_draft: str = "anthropic/claude-sonnet-5"
    model_seo: str = "anthropic/claude-haiku-4.5"
    model_qa: str = "anthropic/claude-opus-4.8"

    # ── Shopify ──────────────────────────────────────────────────
    shopify_store_domain: str = ""
    shopify_access_token: str = ""
    shopify_blog_id: str = ""
    shopify_api_version: str = "2025-01"

    # ── Optional integrations ────────────────────────────────────
    fal_key: str = ""
    dataforseo_login: str = ""
    dataforseo_password: str = ""
    slack_webhook_url: str = ""
    preview_base_url: str = ""

    # ── Tracing ──────────────────────────────────────────────────
    langsmith_api_key: str = ""
    langsmith_tracing: bool = False
    langsmith_project: str = "shopify-blog-pipeline"

    # ── Data store ───────────────────────────────────────────────
    database_url: str = "sqlite:///data/pipeline.db"

    # ── Pipeline behavior ────────────────────────────────────────
    gate_mode: str = "gated"  # "gated" | "auto"
    confidence_threshold: float = 0.75
    cadence: str = "3x/week: Mon/Wed/Fri"
    coverage_target_weeks: int = 4
    word_count_target: int = 1500
    seo_min_score: int = 85

    brand_voice_path: str = "prompts/brand_voice.md"
    # Topics matching these (case-insensitive substring) are blocked by QA.
    banned_topics: list[str] = Field(default_factory=list)

    # ── Topic research inputs (calendar agent) ───────────────────
    niche: str = ""
    seed_keywords: list[str] = Field(default_factory=list)
    competitor_urls: list[str] = Field(default_factory=list)

    # ── Convenience flags ────────────────────────────────────────
    @property
    def has_openrouter(self) -> bool:
        return bool(self.openrouter_api_key)

    @property
    def has_shopify(self) -> bool:
        return bool(self.shopify_store_domain and self.shopify_access_token)

    @property
    def has_images(self) -> bool:
        return bool(self.fal_key)

    @property
    def has_dataforseo(self) -> bool:
        return bool(self.dataforseo_login and self.dataforseo_password)

    @property
    def has_slack(self) -> bool:
        return bool(self.slack_webhook_url)

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
