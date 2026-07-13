from blog_pipeline.utils import html_to_markdown


def test_blockquote_renders_as_markdown_quote():
    html = "<p>Intro.</p><blockquote><p>A quotable line.</p></blockquote><p>More.</p>"
    md = html_to_markdown(html)
    assert "> A quotable line." in md
    # the quote line isn't broken onto its own unprefixed line
    lines = [l for l in md.splitlines() if l.strip()]
    quote_lines = [l for l in lines if "A quotable line." in l]
    assert quote_lines == ["> A quotable line."]


def test_blockquote_does_not_leak_into_following_paragraph():
    html = "<blockquote><p>Quoted.</p></blockquote><p>Not quoted.</p>"
    md = html_to_markdown(html)
    assert "> Not quoted." not in md
    assert "Not quoted." in md
