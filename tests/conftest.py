import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch, tmp_path):
    """Give every test a clean, offline settings object + temp SQLite DB."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "")
    monkeypatch.setenv("FAL_KEY", "")
    monkeypatch.setenv("DATAFORSEO_LOGIN", "")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("GATE_MODE", "auto")

    # Reset cached singletons so the new env is picked up.
    import blog_pipeline.config as config
    import blog_pipeline.db.session as session

    config.get_settings.cache_clear()
    session._engine = None
    session._SessionLocal = None
    yield
    config.get_settings.cache_clear()
    session._engine = None
    session._SessionLocal = None
