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
