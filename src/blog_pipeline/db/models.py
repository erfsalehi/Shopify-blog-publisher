"""SQLAlchemy 2.0 models mirroring the PRD data model (Section 8).

Two clusters:
  * ContentCalendar + CalendarEntry — the rolling topic queue the weekly
    calendar agent maintains. Each entry is mirrored to a Linear issue as
    soon as it's queued (linear_issue_id), so the calendar is visible in
    Linear before drafting ever starts.
  * Article — one row per drafting run, carrying outputs, QA score, cost,
    LangSmith trace id, and the Linear issue the finished draft was synced
    to (there is no separate "publish" step — Linear is the handoff, a
    human publishes from there).

JSON columns hold list/dict fields (keywords, outline, images) so the same
schema works on SQLite and Postgres without a migration.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ArticleStatus(str, enum.Enum):
    """Pipeline execution outcome. `synced` articles wait in Linear for a
    human; `published` ones auto-published live to Shopify (and their Linear
    issue is moved to the published state as a record)."""

    draft = "draft"
    synced = "synced"  # content written to Linear, awaiting human review
    published = "published"  # auto-published live to Shopify
    failed = "failed"


class EntryStatus(str, enum.Enum):
    queued = "queued"
    drafting = "drafting"
    drafted = "drafted"  # article synced to Linear; awaiting human publish
    published = "published"  # article auto-published live to Shopify
    skipped = "skipped"


class RevisionReason(str, enum.Enum):
    pre_refresh = "pre-refresh"
    rollback = "rollback"


class TopicSource(str, enum.Enum):
    manual = "manual"
    auto_researched = "auto-researched"
    # Pre-existing Shopify posts pulled in by `import-existing`. They carry
    # status=published (they are live) but were never drafted here, so they
    # have no seo_score/cost — metrics.py excludes them to keep the pipeline's
    # own numbers honest, while dedup deliberately includes them.
    imported = "imported"


class ContentCalendar(Base):
    __tablename__ = "content_calendar"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cadence: Mapped[str] = mapped_column(String(120), default="3x/week: Mon/Wed/Fri")
    coverage_target_weeks: Mapped[int] = mapped_column(Integer, default=4)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    entries: Mapped[list["CalendarEntry"]] = relationship(
        back_populates="calendar", cascade="all, delete-orphan"
    )


class CalendarEntry(Base):
    __tablename__ = "calendar_entry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    calendar_id: Mapped[int] = mapped_column(ForeignKey("content_calendar.id"))
    scheduled_date: Mapped[date] = mapped_column(Date, index=True)
    topic: Mapped[str] = mapped_column(String(500))
    target_keywords: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[TopicSource] = mapped_column(
        Enum(TopicSource), default=TopicSource.auto_researched
    )
    status: Mapped[EntryStatus] = mapped_column(
        Enum(EntryStatus), default=EntryStatus.queued, index=True
    )
    # Research metadata used for dedup / prioritization.
    search_volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    difficulty: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The Linear issue created for this slot when it was queued (Backlog
    # state, due date = scheduled_date). The article graph updates this same
    # issue in place rather than creating a second one when drafting begins.
    linear_issue_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    linear_identifier: Mapped[str | None] = mapped_column(String(40), nullable=True)
    linear_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    article_id: Mapped[int | None] = mapped_column(
        ForeignKey("article.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    calendar: Mapped["ContentCalendar"] = relationship(back_populates="entries")
    article: Mapped["Article | None"] = relationship(back_populates="entry")


class Article(Base):
    __tablename__ = "article"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic: Mapped[str] = mapped_column(String(500))
    topic_source: Mapped[TopicSource] = mapped_column(
        Enum(TopicSource), default=TopicSource.manual
    )
    target_keywords: Mapped[list] = mapped_column(JSON, default=list)

    outline: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    draft_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    seo_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    seo_description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    handle: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # List of {role, url, alt} dicts.
    images: Mapped[list] = mapped_column(JSON, default=list)

    seo_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    qa_confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    qa_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    status: Mapped[ArticleStatus] = mapped_column(
        Enum(ArticleStatus), default=ArticleStatus.draft, index=True
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    linear_issue_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    linear_identifier: Mapped[str | None] = mapped_column(String(40), nullable=True)
    linear_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Set when the article auto-published live to Shopify.
    shopify_article_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    shopify_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    trace_id: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # LangGraph checkpoint thread id, so a paused (interrupted) run can resume.
    thread_id: Mapped[str | None] = mapped_column(String(120), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )

    entry: Mapped["CalendarEntry | None"] = relationship(back_populates="article")
    revisions: Mapped[list["ArticleRevision"]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )


class ArticleRevision(Base):
    """Snapshot of an article's live Shopify body, taken immediately before
    the refresh agent overwrites it.

    Shopify has no draft-revision concept for an already-published post, so a
    refresh edits public content in place and this snapshot is the only undo.
    A table rather than a `previous_body_html` column because refreshes
    repeat: a column would let the second refresh destroy the original the
    first one was protecting.
    """

    __tablename__ = "article_revision"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("article.id"), index=True, nullable=False
    )
    # The body as it existed on Shopify, fetched live rather than read from
    # our own draft_html — for imported posts we never had the body, and for
    # any post a human may have edited it in admin since we wrote it.
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reason: Mapped[RevisionReason] = mapped_column(
        Enum(RevisionReason), default=RevisionReason.pre_refresh, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    article: Mapped["Article"] = relationship(back_populates="revisions")
