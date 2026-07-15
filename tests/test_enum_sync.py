"""Enum labels added after the first init-db must reach Postgres.

create_all() creates a type once and never alters it, so a value added to a
Python enum later is missing from the database forever and every insert using
it dies with InvalidTextRepresentation. That's exactly how `imported` broke
the first refresh run against Supabase.

The bug is structurally invisible to this suite: SQLite stores enums as plain
text and enforces nothing, so the label logic is unit-tested directly here
rather than through a live insert.
"""

import enum

import pytest

from blog_pipeline.db.models import ArticleStatus, RevisionReason, TopicSource
from blog_pipeline.db.session import (
    _sync_enum_labels,
    get_engine,
    init_db,
    missing_enum_labels,
)


class _Colour(str, enum.Enum):
    red = "red"
    green = "green"
    blue = "blue"


def test_reports_labels_the_database_is_missing():
    assert missing_enum_labels({"red"}, _Colour) == ["green", "blue"]


def test_reports_nothing_when_in_sync():
    assert missing_enum_labels({"red", "green", "blue"}, _Colour) == []


def test_extra_labels_in_the_database_are_left_alone():
    """Dropping a label is destructive and needs a human — only ever add."""
    assert missing_enum_labels({"red", "green", "blue", "purple"}, _Colour) == []


def test_labels_are_names_not_values():
    """SQLAlchemy persists .name, so TopicSource.auto_researched is stored as
    'auto_researched' — never its value, 'auto-researched'."""
    labels = missing_enum_labels(set(), TopicSource)
    assert "auto_researched" in labels
    assert "auto-researched" not in labels


@pytest.mark.parametrize("enum_cls", [TopicSource, ArticleStatus, RevisionReason])
def test_the_real_enums_round_trip(enum_cls):
    """A database already carrying every label needs no change."""
    present = {m.name for m in enum_cls}
    assert missing_enum_labels(present, enum_cls) == []


def test_the_imported_label_that_broke_production_is_detected():
    """The concrete regression: Postgres had the enum from before TopicSource
    gained `imported`, so import-existing failed on every row."""
    before = {"manual", "auto_researched"}
    assert missing_enum_labels(before, TopicSource) == ["imported"]


def test_sqlite_is_a_no_op():
    """SQLite has no enum types to reconcile; init_db must not try."""
    init_db()
    assert _sync_enum_labels(get_engine()) == []
