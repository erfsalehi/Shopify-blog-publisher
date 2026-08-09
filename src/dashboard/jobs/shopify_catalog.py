"""Shopify → `shopify_product`.

The catalogue snapshot everything downstream needs: per-product search
metrics, the SEO pilot cohorts, price experiments, and the competitor
watchlist all key off it.

~2,769 products, so this pages through the Admin API rather than asking for
them all at once, and writes each page before requesting the next. Same
reasoning as the Search Console chunking: a run the proxy kills halfway keeps
what it already got, and the next run resumes cheaply because the write is an
upsert.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from blog_pipeline.tools.shopify import ShopifyClient

from dashboard.config import pipeline
from dashboard.db import get_session
from dashboard.jobs.registry import JobResult, JobSpec, register
from dashboard.models import ShopifyProduct

log = logging.getLogger(__name__)

# 250 is Shopify's per-page maximum for this connection. Fewer, larger pages
# means fewer round trips through a proxy that fails per-request.
_PAGE_SIZE = 250

_QUERY = """
query Products($cursor: String, $size: Int!) {
  products(first: $size, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      handle
      title
      productType
      vendor
      status
      tags
      totalInventory
      priceRangeV2 {
        minVariantPrice { amount currencyCode }
        maxVariantPrice { amount currencyCode }
      }
    }
  }
}
"""


def _storefront_base() -> str:
    """https://drflooring.ca — the public domain, not the myshopify one.

    Search Console reports the canonical public URL, and joining products to
    their search metrics is the entire point of storing a URL here. Building
    it from the *.myshopify.com domain would produce a column that matches
    nothing, which looks exactly like a store with no product traffic.
    """
    return pipeline().store_link_base


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _write_page(session, nodes: list[dict], base: str, now: datetime) -> tuple[int, int]:
    created = updated = 0
    gids = [n.get("id") for n in nodes if n.get("id")]
    existing = {
        p.product_gid: p
        for p in session.query(ShopifyProduct)
        .filter(ShopifyProduct.product_gid.in_(gids))
        .all()
    }
    for node in nodes:
        gid = node.get("id")
        if not gid:
            continue
        price_range = node.get("priceRangeV2") or {}
        low = price_range.get("minVariantPrice") or {}
        high = price_range.get("maxVariantPrice") or {}
        handle = node.get("handle") or ""
        fields = {
            "handle": handle,
            "title": node.get("title") or "",
            "product_type": node.get("productType") or None,
            "vendor": node.get("vendor") or None,
            "status": node.get("status") or None,
            "price_min": _to_float(low.get("amount")),
            "price_max": _to_float(high.get("amount")),
            "currency": low.get("currencyCode") or None,
            "total_inventory": node.get("totalInventory"),
            "tags_json": json.dumps(node.get("tags") or []),
            "online_url": f"{base}/products/{handle}" if base and handle else None,
            "last_seen": now,
        }
        row = existing.get(gid)
        if row is None:
            session.add(ShopifyProduct(product_gid=gid, first_seen=now, **fields))
            created += 1
        else:
            for key, value in fields.items():
                setattr(row, key, value)
            updated += 1
    return created, updated


def sync_shopify_catalog(client: ShopifyClient | None = None) -> JobResult:
    # The credentials check gates *constructing* a client, not using one that
    # was handed in. A caller supplying a client has already solved auth its
    # own way — that's how the tests work, and how a swapped transport would.
    if client is None:
        if not pipeline().has_shopify:
            return JobResult(
                skipped=True,
                skip_reason=(
                    "Shopify isn't configured — set SHOPIFY_STORE_DOMAIN and "
                    "SHOPIFY_ACCESS_TOKEN in .env."
                ),
            )
        client = ShopifyClient()
    base = _storefront_base()
    now = datetime.now(timezone.utc)
    cursor: str | None = None
    pages = created = updated = 0

    while True:
        data = client.graphql(_QUERY, {"cursor": cursor, "size": _PAGE_SIZE})
        block = data.get("products") or {}
        nodes = block.get("nodes") or []
        with get_session() as session:
            page_created, page_updated = _write_page(session, nodes, base, now)
        created += page_created
        updated += page_updated
        pages += 1

        page_info = block.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            # hasNextPage without a cursor would loop forever on page one.
            log.warning("Shopify reported another page but gave no cursor; stopping")
            break

    with get_session() as session:
        total = session.query(ShopifyProduct).count()
        # Anything not touched by this run is no longer in the catalogue.
        # Counted, not deleted — see the model docstring.
        missing = (
            session.query(ShopifyProduct)
            .filter(ShopifyProduct.last_seen < now)
            .count()
        )
        priced = (
            session.query(ShopifyProduct)
            .filter(ShopifyProduct.price_min > 0)
            .count()
        )

    return JobResult(
        rows=created + updated,
        detail={
            "pages": pages,
            "new_products": created,
            "updated_products": updated,
            "total_in_catalogue": total,
            "not_seen_this_run": missing,
            "with_visible_price": priced,
            "storefront_base": base or "(unset — PUBLIC_DOMAIN is empty)",
        },
    )


register(
    JobSpec(
        name="shopify_catalog",
        title="Shopify catalogue snapshot",
        description=(
            "Pulls every product's handle, title, type, vendor, status, price, "
            "inventory and tags. The join key for per-product search metrics, "
            "experiment cohorts and the competitor watchlist."
        ),
        fn=sync_shopify_catalog,
        enabled_key="jobs.shopify_catalog.enabled",
        hour_key="jobs.shopify_catalog.hour",
    )
)
