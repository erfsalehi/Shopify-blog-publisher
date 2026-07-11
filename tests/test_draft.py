from blog_pipeline.agents.draft import _looks_truncated, _strip_bad_links


def test_strip_bad_links_unwraps_placeholder_and_relative():
    html = (
        '<p>Ask <a href="#">D&R Flooring</a> and see '
        '<a href="/pages/x">our page</a>.</p>'
    )
    out = _strip_bad_links(html)
    assert "<a " not in out
    assert "D&R Flooring" in out and "our page" in out


def test_strip_bad_links_keeps_real_http_links():
    html = '<p>See <a href="https://shop.example/collections/laminate">Laminate</a>.</p>'
    out = _strip_bad_links(html)
    assert out == html  # untouched


def test_looks_truncated_detects_incomplete_body():
    assert _looks_truncated("<p>Intro</p><p>Ends mid senten") is True
    assert _looks_truncated("<p>Complete.</p>") is False
