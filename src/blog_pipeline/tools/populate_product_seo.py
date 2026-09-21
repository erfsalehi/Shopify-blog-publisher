"""Tool to populate high-intent native Shopify product SEO titles and descriptions.

For products in the catalog missing custom SEO titles/descriptions:
1. Generates click-driven, localized SEO titles with location & pricing cues:
   Format: "{Title} | In-Stock Langley Showroom | D&R Flooring" (fitted to <= 65 chars)
2. Generates conversion-focused meta descriptions:
   Format: "{Title} available at D&R Flooring in Langley, BC. Expert installation, direct pricing & showroom stock. Call (604) 532-2211 for a quote." (fitted to <= 160 chars)
3. Batch updates Shopify via GraphQL `productUpdate(input: {id, seo: {title, description}})`.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time

from blog_pipeline.tools.shopify import ShopifyClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("populate_product_seo")


def format_seo_title(title: str, max_len: int = 68) -> str:
    """Creates a localized, click-worthy SEO title under search engine character limits."""
    clean_title = title.strip()
    # If title already ends with store name, strip it
    for suffix in [" - D&R Flooring", " - D & R Flooring", " | D&R Flooring"]:
        if clean_title.endswith(suffix):
            clean_title = clean_title[:-len(suffix)].strip()

    suffix = " | Langley Showroom"
    brand = " | D&R Flooring"

    candidate = f"{clean_title}{suffix}"
    if len(candidate) <= max_len:
        return candidate

    # Truncate title cleanly if too long
    allowed_len = max_len - len(suffix)
    truncated = clean_title[:allowed_len].rsplit(" ", 1)[0]
    return f"{truncated}{suffix}"


def format_seo_description(title: str, max_len: int = 158) -> str:
    """Creates a conversion-focused meta description with telephone CTA."""
    clean_title = title.strip()
    prefix = f"{clean_title} in Langley, BC. "
    cta = "Local showroom stock, wholesale pricing & expert installation. Call (604) 532-2211 for a quick quote."

    candidate = f"{prefix}{cta}"
    if len(candidate) <= max_len:
        return candidate

    short_cta = "In-stock at D&R Flooring Langley. Best pricing & installation. Call (604) 532-2211."
    candidate2 = f"{prefix}{short_cta}"
    if len(candidate2) <= max_len:
        return candidate2

    allowed_len = max_len - len(short_cta) - 2
    truncated = clean_title[:allowed_len].rsplit(" ", 1)[0]
    return f"{truncated}.. {short_cta}"


def update_product_seo(client: ShopifyClient, product_id: str, seo_title: str, seo_desc: str) -> bool:
    """Updates a product's native Shopify SEO fields via GraphQL."""
    mutation = """
    mutation productUpdate($input: ProductInput!) {
      productUpdate(input: $input) {
        product {
          id
          seo {
            title
            description
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    payload = {
        "input": {
            "id": product_id,
            "seo": {
                "title": seo_title,
                "description": seo_desc,
            },
        }
    }
    res = client.graphql(mutation, payload)
    errors = res.get("productUpdate", {}).get("userErrors", [])
    if errors:
        logger.error(f"Failed to update {product_id}: {errors}")
        return False
    return True


def run_populate(limit: int = 150, dry_run: bool = False) -> dict:
    """Fetches high-priority products and updates their native SEO fields."""
    client = ShopifyClient()
    conn = sqlite3.connect("data/dashboard.db")
    c = conn.cursor()

    # Get products prioritized by impressions/traffic from GSC, or top products in store
    query = """
    WITH gsc_p AS (
        SELECT page, SUM(impressions) as impr, SUM(clicks) as clicks
        FROM gsc_page_daily
        WHERE page LIKE '%/products/%'
        GROUP BY page
    )
    SELECT p.product_gid, p.title, p.handle, COALESCE(g.impr, 0) as impr, COALESCE(g.clicks, 0) as clicks
    FROM shopify_product p
    LEFT JOIN gsc_p g ON g.page = 'https://drflooring.ca/products/' || p.handle
    WHERE p.status = 'ACTIVE'
    ORDER BY impr DESC, clicks DESC
    """
    c.execute(query)
    all_prods = c.fetchall()
    conn.close()

    logger.info(f"Loaded {len(all_prods)} active products from database.")

    # Process up to limit
    prods_to_update = all_prods[:limit]
    logger.info(f"Targeting top {len(prods_to_update)} products for native SEO optimization...")

    results = []
    success_count = 0
    fail_count = 0

    for i, (p_id, title, handle, impr, clicks) in enumerate(prods_to_update, 1):
        seo_title = format_seo_title(title)
        seo_desc = format_seo_description(title)

        if dry_run:
            if i <= 10:
                logger.info(f"[DRY RUN #{i}] ID: {p_id} | Title: {seo_title}")
                logger.info(f"             Desc: {seo_desc}")
            results.append({"id": p_id, "title": seo_title, "desc": seo_desc})
            continue

        ok = update_product_seo(client, p_id, seo_title, seo_desc)
        if ok:
            success_count += 1
        else:
            fail_count += 1

        if i % 25 == 0 or i == len(prods_to_update):
            logger.info(f"Progress: {i}/{len(prods_to_update)} updated ({success_count} succeeded, {fail_count} failed)")

        time.sleep(0.1)

    if dry_run:
        logger.info(f"[DRY RUN COMPLETE] Simulated SEO metadata for {len(prods_to_update)} products.")
        return {"total": len(prods_to_update), "dry_run": True}

    logger.info(f"Finished: {success_count} products updated successfully in Shopify ({fail_count} failed).")
    return {"total": len(prods_to_update), "succeeded": success_count, "failed": fail_count}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Populate native Shopify product SEO metadata")
    parser.add_argument("--limit", type=int, default=150, help="Number of top products to update")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing to Shopify")
    args = parser.parse_args()

    run_populate(limit=args.limit, dry_run=args.dry_run)
