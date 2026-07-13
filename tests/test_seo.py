from blog_pipeline.agents.seo import _section_word_counts, insert_internal_links, score_seo


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


# ── GEO scoring dimensions (Aggarwal et al., KDD 2024) ──────────────
def test_score_seo_rewards_pull_quote_and_sources():
    body = _sample_article()
    kwargs = dict(
        title="The Best Running Shoes for Every Runner",
        meta_description="A practical guide to choosing running shoes that fit "
        "your terrain, gait, and budget without overspending on hype.",
        primary_keyword="running shoes",
        secondary_keywords=["trail", "fit"],
    )
    without, m_without = score_seo(body, **kwargs)
    with_both, m_with = score_seo(
        body, **kwargs,
        pull_quote="Our fitters find arch support matters more than brand.",
        sources=["American Podiatric Medical Association"],
    )
    assert m_without["has_pull_quote"] is False
    assert m_without["source_count"] == 0
    assert m_with["has_pull_quote"] is True
    assert m_with["source_count"] == 1
    assert with_both > without


def test_section_word_counts_splits_on_h2():
    body = (
        "<p>intro</p>"
        "<h2>One</h2><p>" + " ".join(["word"] * 200) + "</p>"
        "<h2>Two</h2><p>" + " ".join(["word"] * 50) + "</p>"
    )
    counts = _section_word_counts(body)
    assert counts == [200, 50]


def test_section_word_counts_empty_without_h2():
    assert _section_word_counts("<p>just a paragraph, no headings</p>") == []


def test_score_seo_chunk_metric_reflects_compliant_sections():
    # One section in the 150-400 band, one way outside it.
    body = (
        "<p>intro running shoes</p>"
        "<h2>Fit</h2><p>" + " ".join(["running", "shoes", "fit", "word"] * 50) + "</p>"
        "<h2>Terrain</h2><p>too short</p>"
    )
    _, metrics = score_seo(
        body, title="Running Shoes", meta_description="x" * 140,
        primary_keyword="running shoes", secondary_keywords=[],
    )
    assert metrics["chunk_compliant_sections"] == "1/2"
