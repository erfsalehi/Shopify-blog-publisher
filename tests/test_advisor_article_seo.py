"""Blog-title suggestions that carry a button.

The advisor could only ever make `product_seo` executable, so advice about a
blog post's meta title came back as prose with no Apply — the gap this covers.
"""

import pytest

from dashboard import advisor
from dashboard.advisor import _article_handle, _resolve_articles, _valid_action
from dashboard.db import get_session
from dashboard.models import AdvisorAction, BlogArticle, StepKind

HANDLE = "flooring-underlayment-a-comprehensive-guide"
URL = f"https://drflooring.ca/blogs/news/{HANDLE}"
NEW_TITLE = "Flooring Underlayment Guide: Best Types & Langley Prices"


@pytest.fixture
def article(dashboard_db):
    with get_session() as s:
        row = BlogArticle(
            pipeline_id=68, title="Flooring Underlayment: A Comprehensive Guide",
            status="published", shopify_url=URL,
            shopify_article_id="gid://shopify/Article/68",
        )
        s.add(row)
        s.flush()
        return row.id


# ── how the model may name a post ────────────────────────────────
@pytest.mark.parametrize("written", [
    HANDLE,
    f"/blogs/news/{HANDLE}",
    URL,
    f"{URL}/",
    f"{URL}?utm_source=x",
])
def test_a_post_can_be_named_any_of_the_ways_it_appears(written):
    """The context shows posts as paths and the model echoes them back that
    way; rejecting two spellings of one post would lose good advice over
    punctuation."""
    assert _article_handle(written) == HANDLE


# ── resolution ───────────────────────────────────────────────────
def test_a_real_post_resolves_to_its_shopify_id(article):
    found = _resolve_articles([{"handle": f"/blogs/news/{HANDLE}", "seo_title": NEW_TITLE}])
    assert len(found) == 1
    assert found[0]["shopify_article_id"] == "gid://shopify/Article/68"
    assert found[0]["seo_title"] == NEW_TITLE
    # the owner sees the real post title, not the model's guess at it
    assert found[0]["title"] == "Flooring Underlayment: A Comprehensive Guide"


def test_an_invented_handle_resolves_to_nothing(article):
    assert _resolve_articles([{"handle": "a-post-that-never-existed",
                               "seo_title": NEW_TITLE}]) == []


def test_a_post_that_was_never_published_is_not_offered(dashboard_db):
    with get_session() as s:
        s.add(BlogArticle(pipeline_id=9, title="Draft", status="draft",
                          shopify_url=URL, shopify_article_id=None))
    assert _resolve_articles([{"handle": HANDLE, "seo_title": NEW_TITLE}]) == []


def test_an_empty_change_is_not_a_change(article):
    assert _resolve_articles([{"handle": HANDLE, "seo_title": "", "seo_description": ""}]) == []


def test_an_overlong_title_is_refused(article):
    assert _resolve_articles([{"handle": HANDLE, "seo_title": "x" * 500}]) == []


# ── validation: a button is a promise ────────────────────────────
def test_a_resolvable_suggestion_becomes_executable(article):
    action = _valid_action({
        "text": "Retitle the underlayment guide", "kind": "article_seo",
        "articles": [{"handle": HANDLE, "seo_title": NEW_TITLE}],
    })
    assert action.kind == StepKind.article_seo.value
    assert action.payload["articles"][0]["seo_title"] == NEW_TITLE


def test_an_unresolvable_suggestion_keeps_its_text_but_loses_the_button(article):
    action = _valid_action({
        "text": "Retitle the post about nothing", "kind": "article_seo",
        "articles": [{"handle": "not-a-post", "seo_title": NEW_TITLE}],
    })
    assert action.kind == "manual"
    assert action.text == "Retitle the post about nothing"
    assert action.payload == {}


# ── the button gate ──────────────────────────────────────────────
def test_an_article_action_is_a_write_target(dashboard_db):
    row = AdvisorAction(
        scope="blog", text="t", kind=StepKind.article_seo.value,
        payload_json='{"articles": [{"handle": "h", "seo_title": "T"}]}',
    )
    assert row.executable
    assert row.targets == row.articles      # what the template draws a button for
    assert row.products == []


def test_manual_advice_offers_no_target(dashboard_db):
    row = AdvisorAction(scope="blog", text="t", kind="manual")
    assert not row.executable
    assert row.targets == []


# ── the write ────────────────────────────────────────────────────
class FakeShopify:
    def __init__(self, fail_on=()):
        self.calls, self.fail_on, self.closed = [], set(fail_on), False

    def update_article(self, article_id, **kw):
        if article_id in self.fail_on:
            raise RuntimeError("shopify said no")
        self.calls.append((article_id, kw))

    def close(self):
        self.closed = True


@pytest.fixture
def shopify(monkeypatch):
    fake = FakeShopify()
    import blog_pipeline.tools.shopify as mod
    monkeypatch.setattr(mod, "ShopifyClient", lambda *a, **k: fake)
    return fake


def _action(**payload) -> int:
    with get_session() as s:
        row = AdvisorAction(
            scope="blog", text="Retitle it", kind=StepKind.article_seo.value,
            payload_json=payload["json"],
        )
        s.add(row)
        s.flush()
        return row.id


def test_applying_writes_the_meta_title_and_not_the_body(article, shopify):
    import json
    action_id = _action(json=json.dumps({"articles": [{
        "article_id": article, "shopify_article_id": "gid://shopify/Article/68",
        "handle": HANDLE, "title": "Underlayment Guide", "seo_title": NEW_TITLE,
        "seo_description": "",
    }]}))
    row = advisor.run_action(action_id)

    assert row.run_status == "done"
    assert row.status == "done"          # the app did it, so it is done
    gid, kw = shopify.calls[0]
    assert gid == "gid://shopify/Article/68"
    assert kw["seo_title"] == NEW_TITLE
    # the prose is never sent, so the post keeps its body and its history
    assert "body_html" not in kw or kw["body_html"] is None
    assert shopify.closed


def test_a_failed_write_leaves_the_suggestion_open(article, shopify, monkeypatch):
    import json
    shopify.fail_on.add("gid://shopify/Article/68")
    action_id = _action(json=json.dumps({"articles": [{
        "shopify_article_id": "gid://shopify/Article/68", "handle": HANDLE,
        "title": "Underlayment Guide", "seo_title": NEW_TITLE,
    }]}))
    row = advisor.run_action(action_id)

    assert row.run_status == "failed"
    assert row.status == "open"          # the machine failed; the advice stands
    assert "shopify said no" in row.run_result


# ── what the page actually draws ─────────────────────────────────
@pytest.fixture
def client(dashboard_db):
    from fastapi.testclient import TestClient

    from dashboard.web import create_app

    with TestClient(create_app()) as c:
        yield c


def test_the_blog_page_draws_apply_but_not_test_it(client, article):
    """Apply, because the write is real. No "Test it", because an experiment
    needs a control cohort and one article measured against its own past is a
    before/after, not a test."""
    import json

    with get_session() as s:
        s.add(AdvisorAction(
            scope="blog", text="Retitle the underlayment guide",
            kind=StepKind.article_seo.value,
            payload_json=json.dumps({"articles": [{
                "shopify_article_id": "gid://shopify/Article/68",
                "handle": HANDLE, "title": "Flooring Underlayment Guide",
                "seo_title": NEW_TITLE, "seo_description": "",
            }]}),
        ))

    body = client.get("/blog").text
    assert "/advisor/action/1/run" in body        # Apply is rendered
    # the legend explains "Test it", so check for the form, not the phrase
    assert "/advisor/action/1/experiment" not in body
    assert "Blog post" in body                    # payload table header
    assert "Langley Prices" in body               # the proposed title, shown
    assert "article text is not touched" in body  # the confirm says so


def test_manual_blog_advice_still_draws_no_buttons(client, dashboard_db):
    with get_session() as s:
        s.add(AdvisorAction(scope="blog", text="Go and rewrite something",
                            kind="manual"))
    body = client.get("/blog").text
    assert "/advisor/action/1/run" not in body
