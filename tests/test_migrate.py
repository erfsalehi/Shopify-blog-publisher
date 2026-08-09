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

    assert report.copied == {"article": 3, "article_revision": 1}
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

    assert second.copied == {"article": 0, "article_revision": 0}
    assert second.skipped == {"article": 3, "article_revision": 1}
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

    assert report.copied == {"article": 3, "article_revision": 1}
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


def test_search_performance_is_left_behind():
    """30,000 rows of a second opinion. The dashboard keeps its own gsc_*
    tables synced from the API, so copying these buys nothing — and this is
    the sort of 'while we're here' addition that turns a 5-second migration
    into a slow one nobody wants to re-run."""
    from blog_pipeline.migrate import _TABLES

    assert [m.__tablename__ for m in _TABLES] == ["article", "article_revision"]
