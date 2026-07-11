import json
import re

from blog_pipeline.agents.geo import apply_geo, build_jsonld
from blog_pipeline.schemas import FAQItem
from blog_pipeline.utils import html_to_text

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
