"""Copy articles from one database to another.

Written for one specific move — the local `data/pipeline.db` into the Neon
Postgres the deployed Control Center reads — but not written as a throwaway,
because the local pipeline keeps running and keeps producing articles the
deployment can't see. This is re-runnable: it copies what the target is
missing and leaves everything else alone.

Everything the pipeline owns except nothing: the calendar and its queue, the
articles, their revisions, and the two per-article history tables. That's
wider than the first version of this file, which copied only articles —
enough to fill the Blog page's list, and not enough to make it *true*, since
the queue lived in the table left behind.

Rows keep their primary keys. `article_revision.article_id` points at them,
and renumbering on the way across would silently reattach a revision to the
wrong article — the failure would look like an article having someone else's
history, which nothing would flag.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from blog_pipeline.db.models import (
    AiReferral,
    Article,
    ArticleRevision,
    Base,
    CalendarEntry,
    ContentCalendar,
    SearchPerformance,
)
from blog_pipeline.db.session import get_engine, normalize_database_url

#: Insert order, and it is a foreign-key order rather than a preference:
#: a calendar must exist before its entries, and an article before anything
#: that points at one. Getting this wrong doesn't corrupt anything — Postgres
#: refuses the insert — but it does fail the migration halfway.
#:
#: `calendar_entry` is here because the *queue* is the thing that makes the
#: dashboard's Blog page tell the truth. Leaving it behind would move the
#: articles across and still show "Nothing is queued" over a database that
#: has three weeks of topics in it.
#:
#: `search_performance` and `ai_referral` are here for a narrower reason than
#: display: both carry `article_id`, and moving articles without them would
#: strip an article of its own history the moment it arrived.
_TABLES = (
    ContentCalendar,
    Article,
    ArticleRevision,
    CalendarEntry,
    SearchPerformance,
    AiReferral,
)


@dataclass
class MigrationReport:
    copied: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    source_total: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False

    def summary(self) -> str:
        parts = []
        for name in self.copied:
            parts.append(
                f"{name}: {self.copied[name]} copied, "
                f"{self.skipped[name]} already there "
                f"(of {self.source_total[name]})"
            )
        return "; ".join(parts)


def _row_as_dict(obj) -> dict:
    """Column values only — no relationships, no SQLAlchemy instance state."""
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def _resync_sequences(session: Session) -> None:
    """Point each id sequence past the ids we just inserted explicitly.

    Postgres only advances a sequence when it supplies the value itself. Copy
    rows with their own ids and the sequence stays at 1, so the *next* article
    the pipeline writes collides with id 1 and raises a duplicate key error.
    That would happen later, somewhere else, on a machine nobody was
    watching — worth the four lines here.

    No-op on SQLite, which has no sequences to fix.
    """
    if session.bind.dialect.name != "postgresql":
        return
    for model in _TABLES:
        table = model.__tablename__
        session.execute(
            text(
                # pg_get_serial_sequence returns NULL for a table whose id
                # isn't sequence-backed; setval would then error, so skip it.
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 1)) "
                f"WHERE pg_get_serial_sequence('{table}', 'id') IS NOT NULL"
            )
        )


def copy_articles(target_url: str, *, dry_run: bool = False) -> MigrationReport:
    """Copy missing articles and revisions from the local database to `target_url`.

    The source is whatever `DATABASE_URL` currently resolves to — the same
    database every other pipeline command talks to. Only the destination is
    named explicitly, because getting *that* wrong is the one that writes.
    """
    target_engine = create_engine(
        normalize_database_url(target_url), future=True, pool_pre_ping=True
    )
    report = MigrationReport(dry_run=dry_run)

    try:
        # The deployed app creates these at startup, but this must also work
        # against a target nothing has booted against yet.
        Base.metadata.create_all(target_engine, tables=[m.__table__ for m in _TABLES])

        with Session(get_engine()) as source, Session(target_engine) as target:
            for model in _TABLES:
                name = model.__tablename__
                existing = set(target.scalars(select(model.id)).all())
                rows = source.scalars(select(model).order_by(model.id)).all()

                report.source_total[name] = len(rows)
                report.skipped[name] = sum(1 for r in rows if r.id in existing)
                fresh = [r for r in rows if r.id not in existing]
                report.copied[name] = len(fresh)

                if dry_run or not fresh:
                    continue
                # bulk_insert_mappings rather than adding ORM objects: these
                # instances belong to the source session, and the point is to
                # write the columns exactly as they are, defaults included.
                target.bulk_insert_mappings(model, [_row_as_dict(r) for r in fresh])

            if dry_run:
                target.rollback()
                return report

            _resync_sequences(target)
            target.commit()

            # Read back rather than trusting the write — this runs once,
            # against a database whose contents nobody can eyeball afterwards.
            for model in _TABLES:
                name = model.__tablename__
                final = target.scalar(select(func.count()).select_from(model))
                if final < report.source_total[name]:
                    raise RuntimeError(
                        f"{name}: expected at least {report.source_total[name]} "
                        f"rows in the target after copying, found {final}"
                    )
    finally:
        target_engine.dispose()

    return report
