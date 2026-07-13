import json
import re

from blog_pipeline.agents.geo import apply_geo, build_jsonld, render_pull_quote, render_sources
from blog_pipeline.schemas import FAQItem
from blog_pipeline.utils import html_to_markdown, html_to_text

FAQ = [
    FAQItem(question="Is vinyl plank waterproof?", answer="Yes, SPC vinyl is fully waterproof."),
    FAQItem(question="Does laminate scratch?", answer="Quality laminate resists scratching well."),
]


def test_build_jsonld_has_article_and_faqpage():
    block = build_jsonld(title="Flooring Guide", description="A guide.", faq=FAQ)
    assert block.startswith('<script type="application/ld+json">')
    raw = re.search(r">(.*)</script>", block, re.S).group(1).replace("<\\/", "</")
    data = json.loads(raw)
    types = {node["@type"] for node in data["@graph"]}
    assert "Article" in types and "FAQPage" in types
    faqpage = next(n for n in data["@graph"] if n["@type"] == "FAQPage")
    assert len(faqpage["mainEntity"]) == 2


def test_apply_geo_adds_visible_sections_and_jsonld():
    body = "<p>Intro paragraph.</p><h2>Details</h2><p>Body.</p>"
    out = apply_geo(
        body_html=body, title="T", description="D",
        takeaways=["Vinyl is waterproof."], faq=FAQ,
    )
    assert "Key takeaways" in out
    assert "Frequently asked questions" in out
    assert "application/ld+json" in out
    # takeaways box sits before the first real <h2> Details section
    assert out.index("Key takeaways") < out.index("Details")


def test_jsonld_not_counted_as_prose():
    out = apply_geo(
        body_html="<p>Hi.</p>", title="T", description="D", takeaways=[], faq=FAQ,
    )
    text = html_to_text(out)
    assert "FAQPage" not in text and "schema.org" not in text


# ── pull-quote (Aggarwal et al. 2024: +41% citation rate) ──────────
def test_render_pull_quote_wraps_blockquote():
    out = render_pull_quote("Our installers find SPC handles moisture best.")
    assert out.startswith('<blockquote class="pull-quote">')
    assert "Our installers find SPC handles moisture best." in out


def test_render_pull_quote_empty_is_empty():
    assert render_pull_quote("") == ""
    assert render_pull_quote("   ") == ""


def test_pull_quote_survives_html_to_markdown_as_blockquote_line():
    out = render_pull_quote("A quotable line.")
    md = html_to_markdown(out)
    assert md.startswith("> A quotable line.")


# ── sources (Aggarwal et al. 2024: +30% citation rate) ──────────────
def test_render_sources_lists_named_organizations():
    out = render_sources(["National Wood Flooring Association (NWFA)", "ANSI"])
    assert "Sources &amp; standards referenced" in out
    assert "National Wood Flooring Association (NWFA)" in out
    assert "ANSI" in out


def test_render_sources_empty_is_empty():
    assert render_sources([]) == ""
    assert render_sources(["  "]) == ""


def test_build_jsonld_includes_citation_when_sources_given():
    block = build_jsonld(
        title="T", description="D", faq=[], sources=["ANSI", "NWFA"]
    )
    raw = re.search(r">(.*)</script>", block, re.S).group(1).replace("<\\/", "</")
    data = json.loads(raw)
    article = next(n for n in data["@graph"] if n["@type"] == "Article")
    assert article["citation"] == ["ANSI", "NWFA"]


def test_build_jsonld_omits_citation_when_no_sources():
    block = build_jsonld(title="T", description="D", faq=[])
    raw = re.search(r">(.*)</script>", block, re.S).group(1).replace("<\\/", "</")
    data = json.loads(raw)
    article = next(n for n in data["@graph"] if n["@type"] == "Article")
    assert "citation" not in article


# ── apply_geo wiring for quote + sources ────────────────────────────
def test_apply_geo_includes_quote_and_sources():
    body = "<p>Intro.</p><h2>Details</h2><p>Body.</p>"
    out = apply_geo(
        body_html=body, title="T", description="D",
        takeaways=["A takeaway."], faq=FAQ,
        pull_quote="Our team recommends SPC for kitchens.",
        sources=["NWFA"],
    )
    assert "Our team recommends SPC for kitchens." in out
    assert "Sources &amp; standards referenced" in out
    raw = re.search(r'application/ld\+json">(.*)</script>', out, re.S).group(1)
    data = json.loads(raw.replace("<\\/", "</"))
    article = next(n for n in data["@graph"] if n["@type"] == "Article")
    assert article["citation"] == ["NWFA"]
    # ordering: quote sits near the top (after takeaways, before Details), sources near the end
    assert out.index("Our team recommends") < out.index("Details")
    assert out.index("Sources &amp; standards") > out.index("Details")
