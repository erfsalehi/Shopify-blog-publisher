"""Engine + session factory built from DATABASE_URL.

Defaults to SQLite under ./data; swap DATABASE_URL to Postgres for deployment
with no code change. For SQLite we ensure the parent directory exists so a
fresh checkout can `init-db` without manual setup.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from blog_pipeline.config import get_settings
from blog_pipeline.db.models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def normalize_database_url(url: str) -> str:
    """Hosting providers (Railway, Heroku, ...) inject DATABASE_URL as a bare
    `postgres://` or `postgresql://` — that resolves to psycopg2 by default in
    SQLAlchemy 2.0, which we don't install. Rewrite it to explicitly use the
    psycopg (v3) driver so a provider-supplied URL works with no manual edit.
    Any URL that already names a driver (e.g. `postgresql+psycopg://`,
    `sqlite://`) passes through unchanged.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _make_engine() -> Engine:
    raw_url = get_settings().database_url
    if not raw_url.strip():
        # A blank DATABASE_URL reaches here as "" rather than the class
        # default whenever something upstream explicitly sets the env var
        # empty — e.g. a GitHub Actions `${{ secrets.DATABASE_URL }}`
        # referencing a secret that was never added to the repo. Surface
        # that clearly instead of SQLAlchemy's opaque ArgumentError.
        raise RuntimeError(
            "DATABASE_URL is empty. Set it in .env (sqlite:///data/pipeline.db "
            "for local dev) or, for GitHub Actions, add a DATABASE_URL repo "
            "secret pointing at Postgres — see docs/railway-deploy.md."
        )
    url = normalize_database_url(raw_url)
    connect_args: dict = {}
    if url.startswith("sqlite"):
        # Ensure the sqlite file's directory exists (e.g. ./data).
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        connect_args["check_same_thread"] = False
    return create_engine(url, connect_args=connect_args, future=True)


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _make_engine()
    return _engine


def _session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(), expire_on_commit=False, future=True
        )
    return _SessionLocal


@contextmanager
def get_session() -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error."""
    session = _session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    Base.metadata.create_all(get_engine())
