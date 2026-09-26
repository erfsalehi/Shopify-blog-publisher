"""Backfill JSON-LD structured data and local geo internal links on published blog articles.

Enriches articles in Shopify with:
  1. schema.org Article + LocalBusiness (D&R Flooring Langley) + BreadcrumbList + FAQPage
  2. Contextual internal links to:
     - /pages/flooring-in-surrey
     - /pages/flooring-in-langley
     - /pages/custom-stair-nosing-manufacturer-bc
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from bs4 import BeautifulSoup

from blog_pipeline.agents.geo import build_jsonld
from blog_pipeline.agents.seo import insert_internal_links
from blog_pipeline.config import get_settings
from blog_pipeline.schemas import FAQItem
from blog_pipeline.tools.shopify import ShopifyClient

sys.stdout.reconfigure(encoding="utf-8")
log = logging.getLogger(__name__)


def extract_faqs_from_html(soup: BeautifulSoup) -> list[FAQItem]:
    """Extract Q&A pairs from HTML headings and paragraphs."""
    faqs: list[FAQItem] = []

    # 1. Check if existing JSON-LD already has FAQPage
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            raw = s.string or ""
            data = json.loads(raw.replace("<\\/", "</"))
            graph = data.get("@graph") if isinstance(data, dict) else []
            for node in (graph or [data]):
                if node.get("@type") == "FAQPage":
                    for entity in node.get("mainEntity", []):
                        q = re.sub(r"<[^>]+>", "", entity.get("name", "")).strip()
                        ans = re.sub(r"<[^>]+>", "", (entity.get("acceptedAnswer") or {}).get("text", "")).strip()
                        if q and ans:
                            faqs.append(FAQItem(question=q, answer=ans))
        except Exception:
            pass

    if faqs:
        return faqs

    # 2. Extract from visible HTML section
    faq_sec = soup.find("section", class_=re.compile(r"\bfaq\b", re.I))
    if not faq_sec:
        for h in soup.find_all(["h2", "h3"]):
            text = h.get_text().lower()
            if "frequently asked questions" in text or "faq" in text:
                faq_sec = h.parent
                break

    container = faq_sec or soup
    for h3 in container.find_all(["h3", "h4"]):
        q = h3.get_text().strip()
        if "?" in q:
            # Look for adjacent paragraph
            p = h3.find_next_sibling("p")
            if p:
                ans = p.get_text().strip()
                if ans and len(ans) > 10:
                    faqs.append(FAQItem(question=q, answer=ans))

    return faqs


def update_article_geo(
    body_html: str,
    title: str,
    description: str,
    handle: str,
    base_url: str,
) -> tuple[str, bool, dict]:
    """Return (new_body_html, was_changed, stats)."""
    stats = {"links_added": 0, "jsonld_added": False, "jsonld_updated": False}
    article_url = f"{base_url}/blogs/news/{handle}"

    # 1. High-value local link targets
    local_targets = [
        {
            "title": "Flooring in Surrey, BC",
            "url": f"{base_url}/pages/flooring-in-surrey",
            "aliases": [
                "flooring in Surrey",
                "flooring installation in Surrey",
                "flooring in Surrey, BC",
                "Surrey flooring",
                "flooring store in Surrey",
                "Surrey homes",
                "in Surrey",
            ],
        },
        {
            "title": "Flooring in Langley, BC",
            "url": f"{base_url}/pages/flooring-in-langley",
            "aliases": [
                "flooring in Langley",
                "flooring in Langley, BC",
                "Langley flooring showroom",
                "Langley flooring store",
                "flooring showroom in Langley",
                "flooring store in Langley",
                "our showroom in Langley",
                "Langley showroom",
                "Langley homes",
                "in Langley",
            ],
        },
        {
            "title": "Custom Stair Nosing Manufacturer BC",
            "url": f"{base_url}/pages/custom-stair-nosing-manufacturer-bc",
            "aliases": [
                "custom stair nosing",
                "custom stair nose",
                "stair nosing manufacturer",
                "stair nosing in BC",
                "custom flush stair nosing",
                "stair nosing manufacturer in BC",
            ],
        },
    ]

    # Insert internal links into body
    body_with_links, n_links = insert_internal_links(body_html, local_targets, max_links=3)
    stats["links_added"] = n_links

    # 2. If Surrey is not linked, inject the local service card
    if "/pages/flooring-in-surrey" not in body_with_links and "local-service-bar" not in body_with_links:
        service_bar = (
            f'<div class="local-service-bar" style="margin:2em 0;padding:1.25em 1.4em;'
            f'background:#f7f6f4;border:1px solid #e0dedb;border-radius:10px;">'
            f'<p style="margin:0 0 .35em;font-size:1.1em;font-weight:700;color:#1a1a1a;">'
            f'Serving Langley, Surrey &amp; the Fraser Valley</p>'
            f'<p style="margin:0;color:#5c5c5c;font-size:.95em;line-height:1.5;">'
            f'Planning a renovation? Visit our <a href="{base_url}/pages/flooring-in-langley" '
            f'style="color:#1a1a1a;font-weight:600;text-decoration:underline;">Langley Flooring Showroom</a> '
            f'(#103-20551 Langley Bypass) or contact our team for professional '
            f'<a href="{base_url}/pages/flooring-in-surrey" '
            f'style="color:#1a1a1a;font-weight:600;text-decoration:underline;">flooring installation in Surrey</a> '
            f'and across the Lower Mainland. Call us at <a href="tel:+16045322211" '
            f'style="color:#1a1a1a;font-weight:600;">(604) 532-2211</a>.</p></div>'
        )
        # Place before FAQ section if present, else before end
        if '<section class="faq">' in body_with_links:
            body_with_links = body_with_links.replace('<section class="faq">', f'{service_bar}\n<section class="faq">', 1)
        elif '<div class="shop-cta">' in body_with_links:
            body_with_links = body_with_links.replace('<div class="shop-cta">', f'{service_bar}\n<div class="shop-cta">', 1)
        else:
            body_with_links = body_with_links.rstrip() + "\n" + service_bar
        stats["links_added"] += 2

    # 3. Extract FAQs and build rich JSON-LD
    temp_soup = BeautifulSoup(body_with_links, "html.parser")
    faqs = extract_faqs_from_html(temp_soup)

    new_jsonld = build_jsonld(
        title=title,
        description=description,
        faq=faqs,
        url=article_url,
    )

    # Check existing JSON-LD script
    existing_script = temp_soup.find("script", type="application/ld+json")
    if existing_script:
        # Check if it already has LocalBusiness and BreadcrumbList and is valid JSON
        old_raw = existing_script.string or ""
        is_valid = False
        try:
            parsed = json.loads(old_raw.replace("<\\/", "</"))
            is_valid = isinstance(parsed, dict)
        except Exception:
            is_valid = False

        if is_valid and "LocalBusiness" in old_raw and "BreadcrumbList" in old_raw:
            final_body = body_with_links
        else:
            # Replace existing script with upgraded JSON-LD
            existing_script.replace_with(BeautifulSoup(new_jsonld, "html.parser"))
            final_body = str(temp_soup)
            stats["jsonld_updated"] = True
    else:
        # Append new JSON-LD to the end of body
        final_body = body_with_links.rstrip() + "\n" + new_jsonld
        stats["jsonld_added"] = True

    changed = (final_body != body_html)
    return final_body, changed, stats


def run_backfill(dry_run: bool = False, limit: int | None = None) -> None:
    settings = get_settings()
    base_url = settings.store_link_base or "https://drflooring.ca"

    print(f"Connecting to Shopify store: {settings.shopify_store_domain}...")
    client = ShopifyClient()

    # Get articles from the primary blog
    blog_id = settings.shopify_blog_id or "89783599398"
    blog_numeric = blog_id.split("/")[-1]

    # Fetch articles via REST
    import httpx
    rest_client = httpx.Client(
        base_url=f"https://{client.domain}/admin/api/{client.api_version}/",
        headers={"X-Shopify-Access-Token": client.token},
        verify=False,
        timeout=60.0
    )

    print("Fetching published articles...")
    res = rest_client.get(f"blogs/{blog_numeric}/articles.json", params={"limit": 250})
    articles = res.json().get("articles", [])
    published = [a for a in articles if a.get("published_at")]
    print(f"Found {len(published)} published articles (out of {len(articles)} total).")

    if limit:
        published = published[:limit]
        print(f"Limiting to first {limit} articles.")

    updated_count = 0
    skipped_count = 0

    print("\nStarting Backfill:")
    print("=" * 70)

    for i, a in enumerate(published, 1):
        art_id = str(a["id"])
        title = a["title"]
        handle = a.get("handle") or ""
        body = a.get("body_html") or ""
        summary = a.get("summary_html") or ""

        new_body, changed, stats = update_article_geo(
            body_html=body,
            title=title,
            description=summary,
            handle=handle,
            base_url=base_url,
        )

        if not changed:
            skipped_count += 1
            print(f"[{i}/{len(published)}] Skipped (already up-to-date): {title[:50]}")
            continue

        actions = []
        if stats["links_added"] > 0:
            actions.append(f"+{stats['links_added']} local links")
        if stats["jsonld_added"]:
            actions.append("added JSON-LD")
        elif stats["jsonld_updated"]:
            actions.append("upgraded JSON-LD")

        if dry_run:
            print(f"[{i}/{len(published)}] [DRY-RUN] Would update {art_id} ({', '.join(actions)}): {title[:50]}")
            updated_count += 1
            continue

        # Execute live update
        try:
            client.update_article(art_id, body_html=new_body)
            updated_count += 1
            print(f"[{i}/{len(published)}] ✅ Updated {art_id} ({', '.join(actions)}): {title[:50]}")
        except Exception as e:
            print(f"[{i}/{len(published)}] ❌ Error updating {art_id}: {e}")

        time.sleep(0.35)

    client.close()
    rest_client.close()

    print("=" * 70)
    print("Backfill Complete!")
    print(f"Total Updated: {updated_count}")
    print(f"Total Skipped: {skipped_count}")
    print("=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill JSON-LD and Geo links on blog articles.")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without updating Shopify.")
    parser.add_argument("--limit", type=int, default=None, help="Limit the number of articles to process.")
    args = parser.parse_args()

    run_backfill(dry_run=args.dry_run, limit=args.limit)
