import pytest

from blog_pipeline.db.session import normalize_database_url


def test_bare_postgres_scheme_gets_psycopg_driver():
    assert (
        normalize_database_url("postgres://user:pass@host:5432/db")
        == "postgresql+psycopg://user:pass@host:5432/db"
    )


def test_bare_postgresql_scheme_gets_psycopg_driver():
    assert (
        normalize_database_url("postgresql://user:pass@host:5432/db")
        == "postgresql+psycopg://user:pass@host:5432/db"
    )


def test_already_explicit_driver_is_untouched():
    url = "postgresql+psycopg://user:pass@host:5432/db"
    assert normalize_database_url(url) == url


def test_sqlite_url_is_untouched():
    url = "sqlite:///data/pipeline.db"
    assert normalize_database_url(url) == url


def test_blank_database_url_raises_clear_error(monkeypatch):
    """Reproduces the second CI incident: DATABASE_URL: ${{ secrets.DATABASE_URL }}
    with the secret never added leaves the env var set to "" (not absent),
    so the class default never kicks in. Must fail with an actionable message,
    not SQLAlchemy's opaque ArgumentError deep in create_engine."""
    monkeypatch.setenv("DATABASE_URL", "")
    import blog_pipeline.config as config
    import blog_pipeline.db.session as session

    config.get_settings.cache_clear()
    session._engine = None
    try:
        with pytest.raises(RuntimeError, match="DATABASE_URL is empty"):
            session.get_engine()
    finally:
        config.get_settings.cache_clear()
        session._engine = None
