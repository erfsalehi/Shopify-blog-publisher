"""Copying articles into a second database.

Written against SQLite-to-SQLite, which exercises everything except the
Postgres sequence reset — that one has no SQLite equivalent to assert
against, so it's asserted structurally instead (see the last test).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from blog_pipeline.db.models import Article, ArticleRevision, Base
from blog_pipeline.db.session import get_engine, init_db
from blog_pipeline.migrate import copy_articles


@pytest.fixture
def target(tmp_path):
    """An empty second database to copy into."""
    url = f"sqlite:///{(tmp_path / 'target.db').as_posix()}"
    engine = create_engine(url, future=True)
    Base.metadata.create_all(engine)
    yield url, engine
    engine.dispose()


def _seed_source(n: int = 3) -> None:
    init_db()
    with Session(get_engine()) as s:
        for i in range(1, n + 1):
            s.add(Article(id=i * 10, topic=f"topic {i}", title=f"Title {i}"))
        s.flush()
        s.add(ArticleRevision(id=99, article_id=10, body_html="<p>before</p>"))
        s.commit()


def test_articles_and_revisions_are_copied(target):
    url, engine = target
    _seed_source()

    report = copy_articles(url)

    assert report.copied["article"] == 3
    assert report.copied["article_revision"] == 1
    with Session(engine) as t:
        assert t.scalar(select(Article.title).where(Article.id == 10)) == "Title 1"
        assert t.scalar(
            select(ArticleRevision.body_html).where(ArticleRevision.id == 99)
        ) == "<p>before</p>"


def test_primary_keys_survive_the_trip(target):
    """article_revision.article_id points at these. Renumbering on the way
    across would reattach a revision to the wrong article — an article
    wearing someone else's history, which nothing downstream would flag."""
    url, engine = target
    _seed_source()

    copy_articles(url)

    with Session(engine) as t:
        assert sorted(t.scalars(select(Article.id)).all()) == [10, 20, 30]
        rev = t.scalars(select(ArticleRevision)).one()
        assert rev.article_id == 10
        assert t.scalar(
            select(Article.topic).where(Article.id == rev.article_id)
        ) == "topic 1"


def test_running_it_twice_copies_nothing_the_second_time(target):
    """The local pipeline keeps producing articles after the first run, so
    this gets run again — it must top up, not duplicate."""
    url, engine = target
    _seed_source()

    copy_articles(url)
    second = copy_articles(url)

    assert not any(second.copied.values())
    assert second.skipped["article"] == 3
    assert second.skipped["article_revision"] == 1
    with Session(engine) as t:
        assert len(t.scalars(select(Article.id)).all()) == 3


def test_a_new_article_is_picked_up_on_a_later_run(target):
    url, engine = target
    _seed_source()
    copy_articles(url)

    with Session(get_engine()) as s:
        s.add(Article(id=40, topic="written later", title="Later"))
        s.commit()

    report = copy_articles(url)

    assert report.copied["article"] == 1
    with Session(engine) as t:
        assert t.scalar(select(Article.title).where(Article.id == 40)) == "Later"


def test_a_dry_run_writes_nothing(target):
    url, engine = target
    _seed_source()

    report = copy_articles(url, dry_run=True)

    assert report.copied["article"] == 3
    assert report.copied["article_revision"] == 1
    with Session(engine) as t:
        assert t.scalars(select(Article.id)).all() == []


def test_the_id_sequence_is_moved_past_the_copied_rows():
    """Postgres only advances a sequence when it supplies the id itself, so
    rows inserted with their own ids leave it at 1 — and the next article the
    pipeline writes collides with id 1. That failure would surface later, on
    a different machine, in an unrelated command.

    SQLite has no sequences, so there is nothing to observe here. Assert the
    reset is issued for every copied table instead, which is the part a
    future edit could drop."""
    import inspect

    from blog_pipeline import migrate

    src = inspect.getsource(migrate._resync_sequences)
    assert "setval" in src
    assert "for model in _TABLES" in src
    # And that it's actually wired into the write path, not merely defined.
    assert "_resync_sequences(target)" in inspect.getsource(migrate.copy_articles)


def test_the_queue_comes_across_too():
    """Copying articles without `calendar_entry` moves the *history* and
    leaves the *plan* behind — the dashboard would list every published post
    over a "Nothing is queued" banner, above a database holding three weeks
    of topics. That was the actual state before this widened."""
    from blog_pipeline.migrate import _TABLES

    names = [m.__tablename__ for m in _TABLES]
    assert "calendar_entry" in names
    assert "content_calendar" in names


def test_tables_are_ordered_so_foreign_keys_resolve():
    """Postgres refuses an insert whose parent row doesn't exist yet, so a
    wrong order here fails the migration halfway — with the articles moved
    and the queue not."""
    from blog_pipeline.migrate import _TABLES

    names = [m.__tablename__ for m in _TABLES]
    # A calendar before its entries; an article before anything pointing at one.
    assert names.index("content_calendar") < names.index("calendar_entry")
    for dependent in ("article_revision", "calendar_entry",
                      "search_performance", "ai_referral"):
        assert names.index("article") < names.index(dependent), dependent


def test_a_queued_topic_arrives_still_attached_to_its_calendar(target):
    """The end-to-end version of the two structural tests above. A queue
    entry whose `calendar_id` pointed at a row that never came across would
    satisfy every count in the report and still be unreadable — the daily
    drafter joins through it."""
    from datetime import date

    from blog_pipeline.db.models import (
        CalendarEntry, ContentCalendar, EntryStatus, TopicSource,
    )

    url, engine = target
    init_db()
    with Session(get_engine()) as s:
        s.add(ContentCalendar(id=7, cadence="3x/week: Mon/Wed/Fri"))
        s.flush()
        s.add(CalendarEntry(
            id=3, calendar_id=7, scheduled_date=date(2026, 8, 14),
            topic="Acoustic Underlayment for Langley Condos",
            target_keywords=["acoustic underlayment langley"],
            source=TopicSource.auto_researched, status=EntryStatus.queued,
        ))
        s.commit()

    report = copy_articles(url)

    assert report.copied["calendar_entry"] == 1
    assert report.copied["content_calendar"] == 1
    with Session(engine) as t:
        entry = t.scalars(select(CalendarEntry)).one()
        assert entry.topic == "Acoustic Underlayment for Langley Condos"
        assert entry.target_keywords == ["acoustic underlayment langley"]
        # The join the drafter makes must resolve on the far side.
        assert t.get(ContentCalendar, entry.calendar_id) is not None
