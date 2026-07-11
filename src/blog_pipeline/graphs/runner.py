"""Drive the article graph: create the Article row, run it to completion.

Checkpoints persist to a dedicated SQLite file purely for mid-run crash
recovery (LangGraph resumes a partially-run graph from its last checkpoint on
the next `graph.invoke` with the same thread_id) — there's no human-approval
pause to resume from anymore, since the graph always runs straight through to
`sync_linear`.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

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
) -> tuple[int, str | None]:
    """Create the Article row. Returns (article_id, linear_issue_id-of-entry)."""
    linear_issue_id: str | None = None
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
                linear_issue_id = entry.linear_issue_id
    return article_id, linear_issue_id


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
    """Run a full article: outline -> draft -> seo -> images -> qa -> sync to
    Linear. Returns a summary dict with the final status and result."""
    keywords = keywords or []
    linear_issue_id: str | None = None
    if article_id is None:
        article_id, linear_issue_id = create_article_row(topic, keywords, source, entry_id)

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
        "linear_issue_id": linear_issue_id,
        "dry_run": dry_run,
        "cost_usd": 0.0,
    }
    config = {"configurable": {"thread_id": thread_id}}

    cp = _checkpointer()
    try:
        graph = build_article_graph(checkpointer=cp)
        final = graph.invoke(initial, config=config)
    finally:
        _close_checkpointer(cp)

    return {
        "status": final.get("status"),
        "thread_id": thread_id,
        "result": final.get("result"),
        "seo_score": final.get("seo_score"),
        "confidence": final.get("confidence"),
        "cost_usd": final.get("cost_usd"),
    }
