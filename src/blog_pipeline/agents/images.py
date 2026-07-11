"""Image generation stage (Gemini image models via OpenRouter).

For each image slot from the draft: generate an image via OpenRouter using a
cheap Gemini image-output model (token-based billing — fractions of a cent
per image, no fal.ai-style account/billing setup needed beyond an OpenRouter
credit balance), then upload the bytes to Linear's own file storage so the
issue description can embed a real public URL instead of a giant inline
base64 data: URI. Inline images are injected into the body HTML at a
sensible break; the featured image is returned separately for the issue
description header.

The whole stage is optional: if OPENROUTER_API_KEY or Linear isn't
configured, it returns the body unchanged and an empty image list, and the
pipeline continues text-only.
"""

from __future__ import annotations

import base64
import re

import httpx

from blog_pipeline.config import OPENROUTER_BASE_URL, get_settings
from blog_pipeline.schemas import ImageSlot
from blog_pipeline.tools.linear import LinearClient, LinearError


def _generate_one(prompt: str) -> bytes | None:
    """Call the configured OpenRouter image model, return PNG bytes or None."""
    settings = get_settings()
    try:
        resp = httpx.post(
            f"{OPENROUTER_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.openrouter_image_model,
                "messages": [{"role": "user", "content": prompt}],
                "modalities": ["image", "text"],
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        message = resp.json()["choices"][0]["message"]
        images = message.get("images") or []
        if not images:
            return None
        url = (images[0].get("image_url") or {}).get("url", "")
        if not url.startswith("data:"):
            return None
        return base64.b64decode(url.split(",", 1)[1])
    except Exception:
        return None


def _inject_inline(body_html: str, img_html: str, index: int) -> str:
    """Insert an <img> block before the (index+1)-th <h2>, else append."""
    h2s = list(re.finditer(r"<h2", body_html, re.I))
    if len(h2s) > index:
        pos = h2s[index].start()
        return body_html[:pos] + img_html + body_html[pos:]
    return body_html + img_html


def _placeholder_html(slot: ImageSlot) -> str:
    """A bold, bracketed image prompt the user drops into Shopify's AI image
    generator, then replaces with the real image before publishing."""
    return (
        f'<p><strong>[IMAGE - {slot.role}: {slot.prompt} '
        f'(alt: {slot.alt})]</strong></p>'
    )


def place_image_prompts(
    *, body_html: str, image_slots: list[ImageSlot]
) -> tuple[str, list[dict], str | None]:
    """Instead of generating images, drop the prompt for each slot into the
    body as a bold [bracketed] placeholder (featured at the top, inline before
    later H2s). The user generates the real images (e.g. with Shopify's AI) and
    swaps them in before publishing. Returns (body, records, featured_marker)."""
    records: list[dict] = []
    featured_marker: str | None = None
    inline_i = 0
    featured_done = False
    for slot in image_slots:
        block = _placeholder_html(slot)
        records.append({"role": slot.role, "prompt": slot.prompt, "alt": slot.alt})
        if slot.role == "featured" and not featured_done:
            body_html = block + body_html  # featured prompt at the very top
            featured_marker = slot.prompt
            featured_done = True
        else:
            body_html = _inject_inline(body_html, block, inline_i)
            inline_i += 1
    return body_html, records, featured_marker


def generate_images(
    *, body_html: str, image_slots: list[ImageSlot], slug: str = "article"
) -> tuple[str, list[dict], str | None]:
    """Returns (body_html_with_inline_images, image_records, featured_url).

    image_records: [{role, url, alt}] for persistence + the Linear description.
    """
    settings = get_settings()
    if not settings.has_images or not image_slots or not settings.has_linear:
        return body_html, [], None

    client = LinearClient()
    records: list[dict] = []
    featured_url: str | None = None
    inline_i = 0
    try:
        for n, slot in enumerate(image_slots):
            data = _generate_one(slot.prompt)
            if not data:
                continue
            try:
                url = client.upload_file(data, filename=f"{slug}-{n}.png")
            except LinearError:
                continue
            records.append({"role": slot.role, "url": url, "alt": slot.alt})
            if slot.role == "featured" and featured_url is None:
                featured_url = url
            else:
                img_html = f'<figure><img src="{url}" alt="{slot.alt}"></figure>'
                body_html = _inject_inline(body_html, img_html, inline_i)
                inline_i += 1
    finally:
        client.close()

    return body_html, records, featured_url
