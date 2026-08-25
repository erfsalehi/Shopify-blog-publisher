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

    # The dashboard has its own Settings class reading the same .env, and it
    # holds credentials for services that cost money and reach the network
    # (Firecrawl, Windsor). Neutralised here rather than in `dashboard_db`
    # because that fixture isn't autouse: a test that touches dashboard code
    # without asking for a database still gets a live key otherwise, which is
    # how the product-import tests ended up making real Firecrawl calls.
    # Imported defensively so the pipeline suite still runs without the
    # [dashboard] extra installed (see `dashboard_db` below).
    try:
        import dashboard.config as dash_config
    except ImportError:
        dash_config = None
    else:
        monkeypatch.setitem(
            dash_config.DashboardSettings.model_config, "env_file", None
        )
        monkeypatch.setenv("FIRECRAWL_API_KEY", "")
        monkeypatch.setenv("WINDSOR_API_KEY", "")
        dash_config.get_settings.cache_clear()

    # Reset cached singletons so the new env is picked up.
    import blog_pipeline.db.session as session

    config.get_settings.cache_clear()
    session._engine = None
    session._SessionLocal = None
    yield
    config.get_settings.cache_clear()
    if dash_config is not None:
        dash_config.get_settings.cache_clear()
    session._engine = None
    session._SessionLocal = None


@pytest.fixture
def dashboard_db(monkeypatch, tmp_path):
    """A fresh, empty dashboard database for one test.

    Not autouse: only the dashboard tests need it, and importing `dashboard`
    into every pipeline test would make the suite depend on the [dashboard]
    extra being installed.
    """
    import dashboard.config as dash_config
    import dashboard.db as dash_db

    db_path = tmp_path / "dashboard.db"
    monkeypatch.setenv("DASHBOARD_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("DASHBOARD_ENABLE_SCHEDULER", "false")
    monkeypatch.setitem(dash_config.DashboardSettings.model_config, "env_file", None)

    dash_db.reset_engine()
    dash_db.init_db()
    yield db_path
    dash_db.reset_engine()
