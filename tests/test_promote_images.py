from blog_pipeline.agents.images import place_image_prompts
from blog_pipeline.agents.promote import render_shop_cta
from blog_pipeline.schemas import ImageSlot


def test_place_image_prompts_bold_brackets(monkeypatch):
    slots = [
        ImageSlot(role="featured", placement_hint="top", prompt="A cozy room with hardwood floors", alt="hardwood room"),
        ImageSlot(role="inline", placement_hint="mid", prompt="Close-up of vinyl plank", alt="vinyl plank"),
    ]
    body = "<p>Intro.</p><h2>Section</h2><p>Body.</p>"
    out, records, featured = place_image_prompts(body_html=body, image_slots=slots)
    # Each prompt appears as a bold bracketed placeholder.
    assert "<strong>[IMAGE - featured:" in out
    assert "A cozy room with hardwood floors" in out
    assert "Close-up of vinyl plank" in out
    # Featured placeholder is hoisted to the very top.
    assert out.index("A cozy room") < out.index("<p>Intro")
    assert featured == "A cozy room with hardwood floors"
    assert len(records) == 2 and records[0]["prompt"]


def test_render_shop_cta_uses_public_domain(monkeypatch):
    monkeypatch.setenv("SHOP_PROMO", "true")
    monkeypatch.setenv("BUSINESS_NAME", "D&R Flooring")
    monkeypatch.setenv("BUSINESS_LOCATION", "Langley, BC")
    monkeypatch.setenv("PUBLIC_DOMAIN", "drflooring.ca")
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "b98e90.myshopify.com")
    import blog_pipeline.config as config

    config.get_settings.cache_clear()
    cta = render_shop_cta()
    assert "D&amp;R Flooring" in cta or "D&R Flooring" in cta
    assert "https://drflooring.ca/collections/all" in cta
    assert "b98e90.myshopify.com" not in cta
    assert "Langley, BC" in cta
    config.get_settings.cache_clear()


def test_shop_cta_off_when_disabled(monkeypatch):
    monkeypatch.setenv("SHOP_PROMO", "false")
    monkeypatch.setenv("BUSINESS_NAME", "D&R Flooring")
    import blog_pipeline.config as config

    config.get_settings.cache_clear()
    assert render_shop_cta() == ""
    config.get_settings.cache_clear()
