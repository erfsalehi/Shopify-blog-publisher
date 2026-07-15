"""Engine + session factory built from DATABASE_URL.

Defaults to SQLite under ./data; swap DATABASE_URL to Postgres for deployment
with no code change. For SQLite we ensure the parent directory exists so a
fresh checkout can `init-db` without manual setup.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from blog_pipeline.config import get_settings
from blog_pipeline.db.models import (
    ArticleStatus,
    Base,
    EntryStatus,
    RevisionReason,
    TopicSource,
)

# Every Python enum backed by a Postgres ENUM type. The type name is what
# SQLAlchemy derives by default: the class name, lowercased.
_ENUMS = (TopicSource, ArticleStatus, EntryStatus, RevisionReason)

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


def missing_enum_labels(present: set[str], enum_cls) -> list[str]:
    """Labels the Python enum has that the database doesn't yet.

    SQLAlchemy persists a PEP-435 enum by its .name, not its .value — so
    TopicSource.auto_researched is stored as "auto_researched", never
    "auto-researched".
    """
    return [m.name for m in enum_cls if m.name not in present]


def _sync_enum_labels(engine: Engine) -> list[str]:
    """Add enum labels present in Python but missing from Postgres.

    create_all() creates a type the first time and then never touches it, so
    a value added to a Python enum after the first init-db is absent from the
    database forever, and every insert using it dies with
    InvalidTextRepresentation. That's how `imported` broke the first refresh
    run: the type had been created that morning, before TopicSource gained it.

    No test can catch this — SQLite stores enums as plain text and enforces
    nothing, so the suite is green either way. It only exists against real
    Postgres, which is exactly where it hurts.

    This is not a migration system, and shouldn't grow into one. Adding a
    label is the single schema change that is always safe, always additive,
    and idempotent; anything more (renames, drops, column changes) needs a
    real tool and a human deciding.
    """
    if engine.dialect.name != "postgresql":
        return []  # SQLite has no enum types to reconcile.

    added: list[str] = []
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction that then uses
    # the type, so take a dedicated autocommit connection.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for enum_cls in _ENUMS:
            type_name = enum_cls.__name__.lower()
            rows = conn.execute(
                text(
                    "SELECT e.enumlabel FROM pg_enum e "
                    "JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = :name"
                ),
                {"name": type_name},
            ).fetchall()
            if not rows:
                continue  # Type doesn't exist; create_all builds it complete.
            for label in missing_enum_labels({r[0] for r in rows}, enum_cls):
                # Both identifiers come from our own source, never user input.
                conn.execute(
                    text(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{label}'")
                )
                added.append(f"{type_name}.{label}")
    return added


def init_db() -> list[str]:
    """Create anything missing and reconcile enum labels. Returns the labels
    added, so `init-db` can report a schema that had drifted."""
    engine = get_engine()
    Base.metadata.create_all(engine)
    return _sync_enum_labels(engine)
