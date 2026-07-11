"""Store promotion: position the shop as the place to buy inside articles.

Two light touches, both opt-in via `shop_promo`:
  1. A one-line hint fed to the draft agent so the copy naturally treats the
     business as a helpful source for flooring products (no hard sell).
  2. A deterministic "Shop with us" CTA appended near the end of the article,
     linking to the storefront so every post has a clear path to buy.

Links use the public storefront domain (settings.store_link_base), so they
point at the live site rather than the *.myshopify.com URL.
"""

from __future__ import annotations

import html

from blog_pipeline.config import get_settings


def draft_shop_hint() -> str | None:
    """A sentence for the draft prompt, or None when promotion is off."""
    settings = get_settings()
    if not settings.shop_promo or not settings.business_name:
        return None
    loc = f" (based in {settings.business_location})" if settings.business_location else ""
    return (
        f"The publisher, {settings.business_name}{loc}, SELLS the flooring "
        "products and supplies discussed. Where natural, position the store as "
        "a helpful place to buy or get expert advice — a soft, genuine mention "
        "or two, never a hard sell or repeated plugs."
    )


def render_shop_cta() -> str:
    """A closing 'shop with us' call-to-action block, or '' when off."""
    settings = get_settings()
    base = settings.store_link_base
    if not settings.shop_promo or not settings.business_name or not base:
        return ""
    name = html.escape(settings.business_name)
    loc = settings.business_location.strip()
    visit = f" or visit us in {html.escape(loc)}" if loc else ""
    return (
        '<div class="shop-cta"><h2>Shop your flooring project with '
        f"{name}</h2><p>{name} has everything you need to get the job done — "
        "browse our full range of flooring and accessories online, or talk to "
        f'our team about your space. <a href="{base}/collections/all">Shop '
        f"flooring at {name}</a>{visit}.</p></div>"
    )
