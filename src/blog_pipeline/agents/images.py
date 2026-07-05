"""Image generation stage (Flux Schnell via fal.ai).

For each image slot from the draft: generate with Flux Schnell, download the
bytes, upload to Shopify Files, and return an enriched slot carrying the
Shopify file id + URL. Inline images are injected into the body HTML at a
sensible break; the featured image is returned separately for articleCreate.

The whole stage is optional: if FAL_KEY is unset it returns the body unchanged
and an empty image list, and the pipeline continues text-only.
"""

from __future__ import annotations

import re

import httpx

from blog_pipeline.config import get_settings
from blog_pipeline.schemas import ImageSlot
from blog_pipeline.tools.shopify import ShopifyClient

FLUX_MODEL = "fal-ai/flux/schnell"


def _generate_one(prompt: str) -> bytes | None:
    """Call Flux Schnell and return PNG/JPEG bytes, or None on failure."""
    import fal_client

    try:
        result = fal_client.run(
            FLUX_MODEL,
            arguments={
                "prompt": prompt,
                "image_size": "landscape_16_9",
                "num_images": 1,
            },
        )
        images = result.get("images") or []
        if not images:
            return None
        url = images[0]["url"]
        resp = httpx.get(url, timeout=120.0)
        resp.raise_for_status()
        return resp.content
    except Exception:
        return None


def _inject_inline(body_html: str, img_html: str, index: int) -> str:
    """Insert an <img> block before the (index+1)-th <h2>, else append."""
    h2s = list(re.finditer(r"<h2", body_html, re.I))
    if len(h2s) > index:
        pos = h2s[index].start()
        return body_html[:pos] + img_html + body_html[pos:]
    return body_html + img_html


def generate_images(
    *,
    body_html: str,
    image_slots: list[ImageSlot],
    shopify: ShopifyClient | None = None,
    slug: str = "article",
) -> tuple[str, list[dict], str | None]:
    """Returns (body_html_with_inline_images, image_records, featured_file_id).

    image_records: [{role, url, alt, shopify_file_id}] for persistence.
    """
    settings = get_settings()
    if not settings.has_images or not image_slots:
        return body_html, [], None

    # Reuse a Shopify client for uploads if one wasn't passed.
    own_client = False
    if shopify is None:
        if not settings.has_shopify:
            return body_html, [], None
        shopify = ShopifyClient()
        own_client = True

    records: list[dict] = []
    featured_id: str | None = None
    inline_i = 0
    try:
        for n, slot in enumerate(image_slots):
            data = _generate_one(slot.prompt)
            if not data:
                continue
            try:
                uploaded = shopify.upload_image(
                    data, filename=f"{slug}-{n}.png", mime_type="image/png"
                )
            except Exception:
                continue
            rec = {
                "role": slot.role,
                "url": uploaded.get("url"),
                "alt": slot.alt,
                "shopify_file_id": uploaded.get("id"),
            }
            records.append(rec)
            if slot.role == "featured" and featured_id is None:
                featured_id = uploaded.get("id")
            elif uploaded.get("url"):
                img_html = (
                    f'<figure><img src="{uploaded["url"]}" alt="{slot.alt}">'
                    f"</figure>"
                )
                body_html = _inject_inline(body_html, img_html, inline_i)
                inline_i += 1
    finally:
        if own_client:
            shopify.close()

    return body_html, records, featured_id
