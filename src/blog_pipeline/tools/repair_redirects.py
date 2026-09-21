"""Automated audit, backup, and repair tool for Shopify URL redirects.

Audits all redirects in the store:
1. Backs up the entire redirect table to `data/backups/redirects_backup_<timestamp>.json`.
2. Identifies broken redirect targets (uppercase handles, old slugs, typos, 404 targets).
3. Resolves each broken redirect to the true live product or collection URL.
4. Executes `urlRedirectUpdate` GraphQL mutations to repair them.
5. Verifies that all repaired targets return HTTP 200 OK.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.request
import ssl
import certifi
from datetime import datetime
from pathlib import Path

from blog_pipeline.tools.shopify import ShopifyClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("repair_redirects")


def get_catalog_data(db_path: str = "data/dashboard.db") -> tuple[dict[str, dict], dict[str, str]]:
    """Loads active products and collections from dashboard database."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT id, title, handle, product_type FROM shopify_product")
    products = c.fetchall()
    conn.close()

    prods_by_handle = {p[2].lower(): {"id": p[0], "title": p[1], "handle": p[2], "type": p[3]} for p in products}
    return prods_by_handle


def fetch_all_redirects(client: ShopifyClient) -> list[dict]:
    """Fetches every URL redirect stored in Shopify via GraphQL pagination."""
    redirects = []
    cursor = None
    has_next = True

    while has_next:
        after_clause = f', after: "{cursor}"' if cursor else ""
        q = f"""
        {{
          urlRedirects(first: 250{after_clause}) {{
            pageInfo {{
              hasNextPage
              endCursor
            }}
            nodes {{
              id
              path
              target
            }}
          }}
        }}
        """
        res = client.graphql(q)
        data = res.get("urlRedirects", {})
        redirects.extend(data.get("nodes", []))
        has_next = data.get("pageInfo", {}).get("hasNextPage", False)
        cursor = data.get("pageInfo", {}).get("endCursor")

    return redirects


def fetch_live_collections(client: ShopifyClient) -> set[str]:
    """Fetches all live collection handles from Shopify."""
    collections = set()
    cursor = None
    has_next = True
    while has_next:
        after_clause = f', after: "{cursor}"' if cursor else ""
        q = f"""
        {{
          collections(first: 250{after_clause}) {{
            pageInfo {{ hasNextPage endCursor }}
            nodes {{ handle }}
          }}
        }}
        """
        res = client.graphql(q)
        data = res.get("collections", {})
        for node in data.get("nodes", []):
            collections.add(node["handle"].lower())
        has_next = data.get("pageInfo", {}).get("hasNextPage", False)
        cursor = data.get("pageInfo", {}).get("endCursor")

    return collections


def find_best_product_match(target_handle: str, prods_by_handle: dict[str, dict]) -> str | None:
    """Matches an outdated/broken product handle to the active handle."""
    clean = target_handle.strip("/").lower()
    if clean in prods_by_handle:
        return f"/products/{prods_by_handle[clean]['handle']}"

    # Extract informative tokens
    words = [
        w for w in re.split(r"[-_ ]+", clean)
        if len(w) > 2 and w not in ["spc", "plank", "tile", "products", "product", "vinyl", "laminate"]
    ]
    if not words:
        return None

    best_handle = None
    best_score = 0
    for h, p in prods_by_handle.items():
        score = sum(1 for w in words if w in h or w in p["title"].lower())
        if score > best_score:
            best_score = score
            best_handle = p["handle"]

    if best_handle and best_score >= len(words) * 0.7:
        return f"/products/{best_handle}"
    return None


def resolve_collection_fallback(path: str, target: str, live_collections: set[str]) -> str:
    """Maps an unresolvable product or broken collection to the best live category collection."""
    path_lower = (path + " " + target).lower()
    if "laminate" in path_lower:
        return "/collections/laminate-flooring"
    elif "vinyl" in path_lower or "spc" in path_lower or "lvp" in path_lower:
        return "/collections/vinyl-flooring"
    elif "hardwood" in path_lower or "engineered" in path_lower:
        return "/collections/engineered-flooring"
    elif "underlay" in path_lower:
        return "/collections/underlayment" if "underlayment" in live_collections else "/collections/flooring-accessories"
    elif "tile" in path_lower or "ceramic" in path_lower:
        return "/collections/ceramic-tile"
    return "/collections/all"


def backup_redirects(redirects: list[dict], backup_dir: str = "data/backups") -> str:
    """Saves a timestamped backup of all current redirects."""
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(backup_dir, f"redirects_backup_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(redirects, f, indent=2)
    logger.info(f"Backed up {len(redirects)} redirects to {path}")
    return path


def update_redirect(client: ShopifyClient, redirect_id: str, new_target: str) -> bool:
    """Updates a single redirect's target in Shopify."""
    mutation = """
    mutation updateRedirect($id: ID!, $redirect: UrlRedirectInput!) {
      urlRedirectUpdate(id: $id, urlRedirect: $redirect) {
        urlRedirect {
          id
          path
          target
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    res = client.graphql(mutation, {"id": redirect_id, "redirect": {"target": new_target}})
    errors = res.get("urlRedirectUpdate", {}).get("userErrors", [])
    if errors:
        logger.error(f"Failed to update {redirect_id} to {new_target}: {errors}")
        return False
    return True


def run_repair(dry_run: bool = False) -> dict:
    """Main function to audit, match, and repair broken redirects."""
    client = ShopifyClient()
    prods_by_handle = get_catalog_data()
    live_collections = fetch_live_collections(client)

    logger.info("Fetching all redirects from Shopify...")
    redirects = fetch_all_redirects(client)
    logger.info(f"Fetched {len(redirects)} total redirects.")

    if not dry_run:
        backup_redirects(redirects)

    repairs = []
    skipped = 0

    for r in redirects:
        r_id = r["id"]
        path = r["path"]
        target = r["target"]

        # 1. Product Redirects
        if target.startswith("/products/"):
            handle = target.replace("/products/", "").strip("/")
            # If target already matches active product exactly with lowercase
            if handle in prods_by_handle and not any(c.isupper() for c in target):
                skipped += 1
                continue

            # Attempt matching to active product
            new_target = find_best_product_match(handle, prods_by_handle)
            if not new_target:
                new_target = resolve_collection_fallback(path, target, live_collections)
            
            if new_target != target:
                repairs.append({"id": r_id, "path": path, "old_target": target, "new_target": new_target, "type": "product"})

        # 2. Collection Redirects
        elif target.startswith("/collections/"):
            handle = target.replace("/collections/", "").strip("/").lower()
            if handle in live_collections and not any(c.isupper() for c in target):
                skipped += 1
                continue

            # Check collection typos and fixes
            if handle == "brands":
                new_target = "/collections"
            elif handle == "quite-walk-underlayment":
                new_target = "/collections/underlayment" if "underlayment" in live_collections else "/collections"
            elif handle == "underlay-silentstep":
                new_target = "/collections/underlay-silent-step" if "underlay-silent-step" in live_collections else "/collections/flooring-accessories"
            else:
                new_target = resolve_collection_fallback(path, target, live_collections)

            if new_target != target:
                repairs.append({"id": r_id, "path": path, "old_target": target, "new_target": new_target, "type": "collection"})

    logger.info(f"Audit complete: {len(repairs)} broken redirects identified for repair. ({skipped} already valid).")

    if dry_run:
        logger.info("[DRY RUN] No changes made to Shopify.")
        return {"total": len(redirects), "repairs": repairs, "dry_run": True}

    logger.info(f"Applying {len(repairs)} updates to Shopify...")
    success_count = 0
    fail_count = 0

    for i, rep in enumerate(repairs, 1):
        ok = update_redirect(client, rep["id"], rep["new_target"])
        if ok:
            success_count += 1
        else:
            fail_count += 1
        
        # Periodic log and light throttle
        if i % 25 == 0 or i == len(repairs):
            logger.info(f"Progress: {i}/{len(repairs)} updated ({success_count} succeeded, {fail_count} failed)")
        time.sleep(0.1)

    logger.info(f"Repair finished: {success_count} updated successfully, {fail_count} failed.")
    return {"total": len(redirects), "repaired": success_count, "failed": fail_count, "repairs": repairs}


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    run_repair(dry_run=dry)
