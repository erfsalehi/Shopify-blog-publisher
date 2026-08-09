"""Engine and session factory for the dashboard database.

Two shapes, chosen by `DATABASE_URL`:

  * **SQLite** — the local default. Two callers share the file with different
    threads (the FastAPI request handlers and the APScheduler worker running
    a sync), so it needs the two settings applied below: write-ahead logging
    so a long sync doesn't block the UI's reads, and a busy timeout so a
    write that collides with another write waits instead of raising
    `database is locked`.
  * **Postgres** — required once this runs on Vercel, where there is no local
    disk to put a SQLite file on and no single long-lived process to hold one
    open across requests anyway. `normalize_database_url` mirrors the
    pipeline's own (`blog_pipeline.db.session`) so a bare `postgres://` from a
    hosting provider works unedited here too.

    Serverless changes the connection-pooling story: many short-lived
    function invocations opening their own pool would exhaust Postgres' own
    connection limit fast. `NullPool` means every request opens and closes
    its own connection rather than holding a local pool open between
    invocations that may never happen again — pooling is left to the
    database side (Neon's own pooled connection string, the `-pooler` host).
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from dashboard.config import get_settings
from dashboard.models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def normalize_database_url(url: str) -> str:
    """Rewrite a bare `postgres://`/`postgresql://` to the psycopg (v3) driver.

    SQLAlchemy 2.0 defaults an unqualified `postgresql://` to psycopg2, which
    isn't installed here — the `[dashboard]` extra pulls in psycopg. Neon,
    Supabase and most hosts hand out the bare form, so this is the same fix
    `blog_pipeline.db.session.normalize_database_url` applies for the
    pipeline's own database; duplicated rather than imported because the two
    packages' database layers are otherwise independent by design (see
    `db.py`'s own docstring on why the dashboard doesn't share the pipeline's
    schema), and reaching across for four lines would be the one exception.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _make_engine() -> Engine:
    raw = get_settings().database_url
    if not raw.strip():
        raise RuntimeError(
            "DASHBOARD_DATABASE_URL is empty. Set it to sqlite:///data/"
            "dashboard.db for local dev, or a Postgres connection string "
            "(Neon, Supabase, ...) for a Vercel deployment — see "
            "docs/dashboard.md."
        )
    url = normalize_database_url(raw)
    connect_args: dict = {}
    engine_kwargs: dict = {"future": True}

    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # The scheduler thread is not the request thread.
        connect_args["check_same_thread"] = False
        # Wait for a competing writer rather than raising immediately. A GSC
        # sync writing thousands of page rows can hold the write lock for a
        # second or two, which is plenty of time for a page load to collide.
        connect_args["timeout"] = 15.0
    else:
        # Postgres, on Vercel. NullPool — see the module docstring: pooling
        # belongs to Neon's own pooler, not to a per-invocation SQLAlchemy
        # pool that a serverless function will rarely live long enough to
        # reuse. pre_ping guards the one connection each invocation does open
        # against a connection Neon's side already closed while idle.
        engine_kwargs["poolclass"] = NullPool
        engine_kwargs["pool_pre_ping"] = True

    engine = create_engine(url, connect_args=connect_args, **engine_kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            # WAL lets readers proceed during a write. Without it the whole UI
            # stalls behind whatever sync happens to be running.
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


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
    """Create any missing tables. Safe to call on every start, and is."""
    Base.metadata.create_all(get_engine())


def reset_engine() -> None:
    """Drop the cached engine so the next call rebuilds it.

    For tests, which point `DASHBOARD_DATABASE_URL` at a temp file per case.
    """
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
    get_settings.cache_clear()
