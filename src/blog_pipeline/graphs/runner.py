"""Drive the article graph: start runs, pause at the gate, resume on decision.

Checkpoints persist to a dedicated SQLite file so a run interrupted at the
human gate can be resumed by a later CLI invocation (approve/reject) — the
thread_id stored on the Article row is the handle back into the checkpoint.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from blog_pipeline.config import get_settings
from blog_pipeline.db import Article, ArticleStatus, get_session
from blog_pipeline.db.models import TopicSource
from blog_pipeline.graphs.article_graph import build_article_graph

_CHECKPOINT_PATH = Path("data/checkpoints.sqlite")


def _checkpointer() -> SqliteSaver:
    _CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    import sqlite3

    conn = sqlite3.connect(str(_CHECKPOINT_PATH), check_same_thread=False)
    return SqliteSaver(conn)


def _close_checkpointer(cp: SqliteSaver) -> None:
    conn = getattr(cp, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def create_article_row(
    topic: str,
    keywords: list[str],
    source: TopicSource = TopicSource.manual,
    entry_id: int | None = None,
) -> int:
    with get_session() as s:
        article = Article(
            topic=topic,
            topic_source=source,
            target_keywords=keywords,
            status=ArticleStatus.draft,
        )
        s.add(article)
        s.flush()
        article_id = article.id
        if entry_id is not None:
            from blog_pipeline.db.models import CalendarEntry, EntryStatus

            entry = s.get(CalendarEntry, entry_id)
            if entry:
                entry.article_id = article_id
                entry.status = EntryStatus.drafting
    return article_id


def _summarize(state: dict, interrupted: bool, thread_id: str) -> dict:
    if interrupted:
        return {"status": "pending_review", "thread_id": thread_id,
                "interrupt": state}
    return {
        "status": state.get("status"),
        "thread_id": thread_id,
        "result": state.get("result"),
        "seo_score": state.get("seo_score"),
        "confidence": state.get("confidence"),
        "cost_usd": state.get("cost_usd"),
    }


def run_article(
    topic: str,
    keywords: list[str] | None = None,
    *,
    source: TopicSource = TopicSource.manual,
    entry_id: int | None = None,
    competitor_headers: list[str] | None = None,
    dry_run: bool = False,
    article_id: int | None = None,
) -> dict:
    """Start (or continue) a full article run. Returns a summary dict.

    If the run hits the human gate it returns status 'pending_review' with the
    interrupt payload; call `resume_article` later to finish it.
    """
    keywords = keywords or []
    if article_id is None:
        article_id = create_article_row(topic, keywords, source, entry_id)

    thread_id = f"article-{article_id}-{uuid.uuid4().hex[:8]}"
    with get_session() as s:
        obj = s.get(Article, article_id)
        if obj:
            obj.thread_id = thread_id

    initial: dict = {
        "article_id": article_id,
        "topic": topic,
        "target_keywords": keywords,
        "competitor_headers": competitor_headers or [],
        "dry_run": dry_run,
        "cost_usd": 0.0,
    }
    config = {"configurable": {"thread_id": thread_id}}

    cp = _checkpointer()
    try:
        graph = build_article_graph(checkpointer=cp)
        final = graph.invoke(initial, config=config)
        snapshot = graph.get_state(config)
        interrupted = bool(snapshot.next)
    finally:
        _close_checkpointer(cp)
    return _summarize(final, interrupted, thread_id)


def resume_article(article_id: int, action: str, note: str = "") -> dict:
    """Resume a paused run at the human gate with approve/reject."""
    with get_session() as s:
        obj = s.get(Article, article_id)
        if obj is None:
            raise ValueError(f"Article {article_id} not found.")
        thread_id = obj.thread_id
    if not thread_id:
        raise ValueError(f"Article {article_id} has no active run to resume.")

    config = {"configurable": {"thread_id": thread_id}}
    cp = _checkpointer()
    try:
        graph = build_article_graph(checkpointer=cp)
        final = graph.invoke(
            Command(resume={"action": action, "note": note}), config=config
        )
        snapshot = graph.get_state(config)
        interrupted = bool(snapshot.next)
    finally:
        _close_checkpointer(cp)
    return _summarize(final, interrupted, thread_id)
