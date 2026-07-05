from blog_pipeline.agents.seo import insert_internal_links, score_seo


def _sample_article(keyword="running shoes"):
    return (
        f"<p>Choosing the best {keyword} matters for every runner. "
        f"Good {keyword} protect your joints.</p>"
        "<h2>Fit and comfort</h2>"
        "<p>Fit is the single most important factor when you shop. "
        "Try shoes on in the afternoon when feet are largest.</p>"
        "<h2>Terrain</h2>"
        "<p>Road and trail demand different outsoles. Match the shoe to "
        "where you actually run each week for the best results.</p>"
    )


def test_score_seo_rewards_good_article():
    body = _sample_article()
    score, metrics = score_seo(
        body,
        title="The Best Running Shoes for Every Runner",
        meta_description="A practical guide to choosing running shoes that fit "
        "your terrain, gait, and budget without overspending on hype.",
        primary_keyword="running shoes",
        secondary_keywords=["trail", "fit"],
    )
    assert 0 <= score <= 100
    assert metrics["kw_in_title"] is True
    assert metrics["kw_in_intro"] is True
    assert metrics["h2_count"] == 2


def test_score_seo_penalizes_missing_keyword():
    body = "<p>Generic content with no target term.</p><h2>Section</h2><p>More.</p>"
    score, metrics = score_seo(
        body, title="Unrelated Title", meta_description="short",
        primary_keyword="running shoes", secondary_keywords=[],
    )
    assert metrics["kw_in_title"] is False
    assert score < 85


def test_insert_internal_links_links_first_occurrence():
    body = "<p>We love hiking boots for the trail. Hiking boots last years.</p>"
    targets = [{"title": "hiking boots", "url": "https://shop/products/boots"}]
    linked, n = insert_internal_links(body, targets)
    assert n == 1
    assert linked.count("<a href=") == 1
    assert "https://shop/products/boots" in linked


def test_insert_internal_links_respects_max():
    body = "<p>apples bananas cherries dates elderberries figs</p>"
    targets = [
        {"title": w, "url": f"https://shop/{w}"}
        for w in ["apples", "bananas", "cherries", "dates", "elderberries", "figs"]
    ]
    _, n = insert_internal_links(body, targets, max_links=3)
    assert n == 3
