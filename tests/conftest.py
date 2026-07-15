import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch, tmp_path):
    """Give every test a clean, offline settings object + temp SQLite DB."""
    import blog_pipeline.config as config

    # Ignore the developer's real .env entirely. Two reasons: anything this
    # fixture doesn't explicitly override would otherwise leak in, and the
    # `setenv(X, "")` calls below mean "not configured" — but a blank env var
    # now reads as unset and falls through to the next source, which would be
    # .env and its live keys (see _BlankAsUnset in config.py).
    monkeypatch.setitem(config.Settings.model_config, "env_file", None)

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("LINEAR_API_KEY", "")
    monkeypatch.setenv("LINEAR_TEAM", "")
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "")
    # Pin the live/hidden switch so tests don't inherit the real .env value.
    monkeypatch.setenv("SHOPIFY_PUBLISH_LIVE", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("DATAFORSEO_LOGIN", "")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")

    # Reset cached singletons so the new env is picked up.
    import blog_pipeline.db.session as session

    config.get_settings.cache_clear()
    session._engine = None
    session._SessionLocal = None
    yield
    config.get_settings.cache_clear()
    session._engine = None
    session._SessionLocal = None
