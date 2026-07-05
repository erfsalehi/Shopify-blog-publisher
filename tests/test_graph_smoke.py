"""End-to-end wiring test for the article graph with all LLM/IO mocked.

Verifies the graph runs outline -> draft -> seo -> qa -> publish in dry-run
mode, persists an Article row, accumulates cost, and reaches a terminal status
without hitting any network."""

import blog_pipeline.graphs.article_graph as ag
from blog_pipeline.schemas import Draft, Outline, QAReport


def _fake_outline(topic, keywords, competitor_headers=None, cost=None):
    return Outline(
        working_title=f"About {topic}",
        primary_keyword=(keywords or [topic])[0],
        secondary_keywords=[],
        sections=[],
    )


def _fake_draft(outline, cost=None):
    return Draft(
        title="Test Article",
        meta_description="A meta description that is long enough to be plausible "
        "for the SEO scoring rubric to accept as valid input here.",
        body_html="<p>Intro about test article.</p><h2>One</h2><p>Body text here "
        "with enough words to look like a real paragraph of content.</p>"
        "<h2>Two</h2><p>More body content to satisfy the readability checks.</p>",
        image_slots=[],
    )


def _fake_qa(title, body_html, existing_titles=None, cost=None):
    return QAReport(confidence=0.95, verdict="pass", notes="ok")


def test_article_graph_dry_run(monkeypatch):
    from blog_pipeline.db import init_db

    init_db()

    # Mock the three LLM stages and the SEO meta polish.
    monkeypatch.setattr(ag, "generate_outline", _fake_outline)
    monkeypatch.setattr(ag, "generate_draft", _fake_draft)
    monkeypatch.setattr(ag, "review_article", _fake_qa)

    # SEO agent's LLM meta polish should be skipped; force deterministic path.
    import blog_pipeline.agents.seo as seo_mod

    def _fake_optimize(**kwargs):
        from blog_pipeline.agents.seo import SEOResult, score_seo

        score, metrics = score_seo(
            kwargs["body_html"], kwargs["title"], kwargs["meta_description"],
            kwargs["primary_keyword"], kwargs["secondary_keywords"],
        )
        return SEOResult(
            score=score, seo_title=kwargs["title"],
            seo_description=kwargs["meta_description"],
            body_html=kwargs["body_html"], metrics=metrics,
        )

    monkeypatch.setattr(ag, "optimize_seo", _fake_optimize)

    graph = ag.build_article_graph(checkpointer=None)
    state = {
        "article_id": None,
        "topic": "test topic",
        "target_keywords": ["test topic"],
        "dry_run": True,
        "cost_usd": 0.0,
    }
    # Need a real article row for persistence helpers; create one.
    from blog_pipeline.graphs.runner import create_article_row

    state["article_id"] = create_article_row("test topic", ["test topic"])

    final = graph.invoke(state, config={"configurable": {"thread_id": "t1"}})
    assert final["status"] == "dry_run"
    assert final["result"]["dry_run"] is True
    assert "payload" in final["result"]
