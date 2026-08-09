"""Writing a product's SEO title/description via `productUpdate`.

Two traps, both of which fail *silently* — the mutation returns success and
the field simply isn't what you sent. Neither is discoverable from the API
response unless you look for it, which is why this module verifies the echo
rather than trusting `userErrors` being empty.

**Trap 1 — `seo` is replaced wholesale, not merged.** Sending only
`seo.title` blanks the existing `seo.description`. So both fields go on every
write, and the current values are read first to supply whichever one isn't
changing.

**Trap 2 — a `seo.title` equal to the product title is dropped.** Shopify
treats "the SEO title is the same as the product title" as "there is no
custom SEO title" and stores null. No error, no warning. For a title
experiment that is the whole treatment silently not being applied, so it is
detected and raised.
"""

from __future__ import annotations

import logging

from blog_pipeline.tools.shopify import ShopifyClient, ShopifyError

log = logging.getLogger(__name__)

_READ = """
query ProductSeo($id: ID!) {
  product(id: $id) {
    id
    title
    seo { title description }
  }
}
"""

_UPDATE = """
mutation UpdateSeo($input: ProductInput!) {
  productUpdate(input: $input) {
    product { id title seo { title description } }
    userErrors { field message }
  }
}
"""


class SeoWriteError(ShopifyError):
    pass


def read_seo(client: ShopifyClient, product_gid: str) -> dict:
    data = client.graphql(_READ, {"id": product_gid})
    product = data.get("product")
    if not product:
        raise SeoWriteError(f"No product found for {product_gid}")
    seo = product.get("seo") or {}
    return {
        "title": product.get("title"),
        "seo_title": seo.get("title"),
        "seo_description": seo.get("description"),
    }


def write_seo(
    client: ShopifyClient,
    product_gid: str,
    *,
    seo_title: str | None = None,
    seo_description: str | None = None,
) -> dict:
    """Set either or both SEO fields, preserving the one not being changed.

    Returns {"before": ..., "after": ...} with the values Shopify actually
    stored — read back from the mutation's own response, not assumed.
    """
    before = read_seo(client, product_gid)

    # Trap 1: `seo` is replaced, not merged. Whichever field isn't being
    # changed has to be sent back as it was, or it is erased.
    next_title = before["seo_title"] if seo_title is None else seo_title
    next_description = (
        before["seo_description"] if seo_description is None else seo_description
    )

    # Trap 2: caught before the write, because after the write it looks
    # identical to a field that was never set.
    if next_title is not None and next_title.strip() == (before["title"] or "").strip():
        raise SeoWriteError(
            "Shopify silently discards an SEO title identical to the product "
            f"title ({before['title']!r}) — it stores null and reports no "
            "error. Use a different SEO title, or the experiment's treatment "
            "will not actually be applied."
        )

    data = client.graphql(_UPDATE, {
        "input": {
            "id": product_gid,
            "seo": {"title": next_title, "description": next_description},
        }
    })
    payload = data.get("productUpdate") or {}
    errors = payload.get("userErrors") or []
    if errors:
        raise SeoWriteError(f"productUpdate userErrors: {errors}")

    product = payload.get("product") or {}
    stored = product.get("seo") or {}
    after = {
        "title": product.get("title"),
        "seo_title": stored.get("title"),
        "seo_description": stored.get("description"),
    }

    # Verify the echo. Both traps present as "success" with the wrong value,
    # so an unverified write here would report an applied treatment that isn't.
    if next_title is not None and after["seo_title"] != next_title:
        raise SeoWriteError(
            f"Shopify stored SEO title {after['seo_title']!r}, not the "
            f"{next_title!r} that was sent. The write reported no errors, so "
            "this is one of Shopify's silent rejections."
        )
    if next_description is not None and after["seo_description"] != next_description:
        raise SeoWriteError(
            f"Shopify stored SEO description {after['seo_description']!r}, "
            f"not what was sent."
        )

    log.info("updated SEO for %s", product_gid)
    return {"before": before, "after": after}
