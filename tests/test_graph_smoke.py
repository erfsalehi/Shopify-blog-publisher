"""End-to-end wiring test for the article graph with all LLM/IO mocked.

Verifies the graph runs outline -> draft -> seo -> qa -> sync_linear in
dry-run mode, persists an Article row, accumulates cost, and reaches a
terminal status without hitting any network."""

import blog_pipeline.graphs.article_graph as ag
from blog_pipeline.schemas import Draft, Outline, QAReport


def _fake_outline(topic, keywords, competitor_headers=None, cost=None):
    return Outline(
        working_title=f"About {topic}",
        primary_keyword=(keywords or [topic])[0],
        secondary_keywords=[],
        sections=[],
    )


def _fake_draft(outline, competitor_headers=None, cost=None):
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
    # Short fake body scores below the pass mark; keep the revise pass a no-op.
    monkeypatch.setattr(ag, "revise_article", lambda **kw: (kw["body_html"], kw.get("pull_quote", ""), kw.get("sources", [])))

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

    state["article_id"], state["linear_issue_id"] = create_article_row(
        "test topic", ["test topic"]
    )

    final = graph.invoke(state, config={"configurable": {"thread_id": "t1"}})
    assert final["status"] == "dry_run"
    assert final["result"]["dry_run"] is True
    # No Shopify configured in tests -> Linear-only path, payload previewed.
    assert "linear_payload" in final["result"]


def test_confident_pass_auto_publishes_to_shopify(monkeypatch):
    """With Shopify configured + a confident QA pass, the real (live) run
    routes through the publish node and reaches ArticleStatus.published,
    with Shopify + Linear fully mocked."""
    from blog_pipeline.db import Article, ArticleStatus, get_session, init_db

    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "test-store.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "shpat_test")
    monkeypatch.setenv("LINEAR_API_KEY", "lin_test")
    monkeypatch.setenv("LINEAR_TEAM", "Content")
    import blog_pipeline.config as config

    config.get_settings.cache_clear()
    init_db()

    monkeypatch.setattr(ag, "generate_outline", _fake_outline)
    monkeypatch.setattr(ag, "generate_draft", _fake_draft)
    monkeypatch.setattr(ag, "review_article", _fake_qa)

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
    # Short fake body scores below the pass mark; keep the revise pass a no-op.
    monkeypatch.setattr(ag, "revise_article", lambda **kw: (kw["body_html"], kw.get("pull_quote", ""), kw.get("sources", [])))

    # Stub the Shopify client so no network call happens.
    from blog_pipeline.tools.shopify import PublishResult

    class _FakeShopify:
        def __init__(self, *a, **k):
            pass

        def create_article(self, **kwargs):
            return PublishResult(
                article_id="gid://shopify/Article/1",
                handle="test-article",
                url="https://test-store.myshopify.com/blogs/news/test-article",
            )

        def close(self):
            pass

    monkeypatch.setattr(ag, "ShopifyClient", _FakeShopify)

    # Stub the Linear client so the sync is a no-op returning an IssueResult.
    from blog_pipeline.tools.linear import IssueResult

    class _FakeLinear:
        def __init__(self, *a, **k):
            pass

        def update_issue(self, *a, **k):
            return IssueResult(id="iss1", identifier="CON-1", url="https://linear.app/x")

        def create_issue(self, *a, **k):
            return IssueResult(id="iss1", identifier="CON-1", url="https://linear.app/x")

        def add_comment(self, *a, **k):
            pass

        def close(self):
            pass

    monkeypatch.setattr(ag, "LinearClient", _FakeLinear)

    from blog_pipeline.graphs.runner import create_article_row

    article_id, linear_issue_id = create_article_row("test topic", ["test topic"])

    graph = ag.build_article_graph(checkpointer=None)
    final = graph.invoke(
        {
            "article_id": article_id,
            "topic": "test topic",
            "target_keywords": ["test topic"],
            "linear_issue_id": linear_issue_id,
            "dry_run": False,
            "cost_usd": 0.0,
        },
        config={"configurable": {"thread_id": "t2"}},
    )

    assert final["status"] == "published"
    assert final["result"]["shopify_url"].endswith("/test-article")
    with get_session() as s:
        row = s.get(Article, article_id)
        assert row.status == ArticleStatus.published
        assert row.shopify_article_id == "gid://shopify/Article/1"

    # ── hidden-draft mode: same confident pass, SHOPIFY_PUBLISH_LIVE=false ──
    monkeypatch.setenv("SHOPIFY_PUBLISH_LIVE", "false")
    monkeypatch.setenv("LINEAR_REVIEW_STATE", "Ready to Review")
    config.get_settings.cache_clear()
    article_id2, issue2 = create_article_row("test topic two", ["test topic two"])
    final2 = graph.invoke(
        {
            "article_id": article_id2,
            "topic": "test topic two",
            "target_keywords": ["test topic two"],
            "linear_issue_id": issue2,
            "dry_run": False,
            "cost_usd": 0.0,
        },
        config={"configurable": {"thread_id": "t3"}},
    )
    # Real Shopify article created, but not live -> synced (awaiting manual publish).
    assert final2["status"] == "synced"
    assert final2["result"]["linear_state"] == "Ready to Review"
    with get_session() as s:
        row2 = s.get(Article, article_id2)
        assert row2.status == ArticleStatus.synced
        assert row2.shopify_article_id == "gid://shopify/Article/1"
        assert row2.published_at is None

    config.get_settings.cache_clear()
