"""Regression test for a real production incident: GitHub Actions passes an
unset `vars.X` through to the job as an env var set to the literal empty
string "" (not simply absent), which crashed both scheduled workflow runs
(SEED_KEYWORDS in weekly-calendar.yml).

Blank values are now normalised to "unset" at the settings-source level, so
every field falls back to its class default — see _BlankAsUnset in config.py.
These stay plain `str` with a derived `_list` property for a separate reason:
pydantic-settings JSON-decodes list[str] env vars, so a list field would need
'["a", "b"]' rather than the a,b form used throughout .env.example.
"""

import blog_pipeline.config as config


def test_empty_string_env_vars_do_not_crash_settings(monkeypatch):
    # Reproduces exactly what `SEED_KEYWORDS: ${{ vars.SEED_KEYWORDS }}` sends
    # when the repo variable is unset. These four default to "", so blank and
    # unset land on the same empty list either way.
    monkeypatch.setenv("SEED_KEYWORDS", "")
    monkeypatch.setenv("COMPETITOR_URLS", "")
    monkeypatch.setenv("BANNED_TOPICS", "")
    monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "")
    config.get_settings.cache_clear()
    try:
        settings = config.get_settings()  # must not raise SettingsError
        assert settings.seed_keywords_list == []
        assert settings.competitor_urls_list == []
        assert settings.banned_topics_list == []
        assert settings.whatsapp_allowed_list == []
    finally:
        config.get_settings.cache_clear()


def test_blank_fallback_models_restores_the_default_chain(monkeypatch):
    """llm_fallback_models is the only one of these with a non-empty default,
    so it's where "blank == unset" is observable: an unset LLM_FALLBACK_MODELS
    now yields the intended retry chain. It used to yield [], quietly disabling
    fallbacks on exactly the free-tier runs that need them most."""
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "")
    config.get_settings.cache_clear()
    try:
        assert config.get_settings().llm_fallback_models_list == [
            "gemini-2.5-flash",
            "gemini-3.1-flash-lite",
        ]
    finally:
        config.get_settings.cache_clear()


def test_comma_separated_env_vars_parse_correctly(monkeypatch):
    monkeypatch.setenv("SEED_KEYWORDS", "hardwood flooring, laminate flooring ,")
    monkeypatch.setenv("BANNED_TOPICS", "asbestos,mold removal")
    config.get_settings.cache_clear()
    try:
        settings = config.get_settings()
        assert settings.seed_keywords_list == ["hardwood flooring", "laminate flooring"]
        assert settings.banned_topics_list == ["asbestos", "mold removal"]
    finally:
        config.get_settings.cache_clear()
