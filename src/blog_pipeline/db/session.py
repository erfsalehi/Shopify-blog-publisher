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


def _make_engine() -> Engine:
    url = get_settings().database_url
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
