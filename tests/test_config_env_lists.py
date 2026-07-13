"""Regression test for a real production incident: GitHub Actions passes an
unset `vars.X` through to the job as an env var set to the literal empty
string "" (not simply absent). pydantic-settings' default list[str] env
decoding tries to JSON-parse that and raises SettingsError, which crashed
both scheduled workflow runs (SEED_KEYWORDS in weekly-calendar.yml). These
fields are plain `str` with a derived `_list` property specifically so an
empty string degrades to an empty list instead of blowing up Settings().
"""

import blog_pipeline.config as config


def test_empty_string_env_vars_do_not_crash_settings(monkeypatch):
    # Reproduces exactly what `SEED_KEYWORDS: ${{ vars.SEED_KEYWORDS }}` sends
    # when the repo variable is unset.
    monkeypatch.setenv("SEED_KEYWORDS", "")
    monkeypatch.setenv("COMPETITOR_URLS", "")
    monkeypatch.setenv("BANNED_TOPICS", "")
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "")
    monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "")
    config.get_settings.cache_clear()
    try:
        settings = config.get_settings()  # must not raise SettingsError
        assert settings.seed_keywords_list == []
        assert settings.competitor_urls_list == []
        assert settings.banned_topics_list == []
        assert settings.llm_fallback_models_list == []
        assert settings.whatsapp_allowed_list == []
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
