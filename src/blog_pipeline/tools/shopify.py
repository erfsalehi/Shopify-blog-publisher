"""Shopify Admin GraphQL client.

Covers exactly what the pipeline needs:
  * default_blog_id   — resolve the store's first blog if none configured
  * list_published    — published article titles/handles for dedup + internal links
  * list_link_targets — products/pages/articles used as internal-link anchors
  * upload_image      — stagedUploadsCreate -> PUT -> fileCreate (returns file id/url)
  * create_article    — articleCreate mutation, published immediately

The client raises ShopifyError on GraphQL userErrors so callers can mark the
Article row failed with a real reason. `dry_run` short-circuits create_article
and returns the payload instead of calling the API.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from blog_pipeline.config import get_settings


class ShopifyError(RuntimeError):
    pass


@dataclass
class PublishResult:
    article_id: str | None
    handle: str | None
    url: str | None
    dry_run: bool = False
    payload: dict | None = None


class ShopifyClient:
    def __init__(
        self,
        domain: str | None = None,
        token: str | None = None,
        api_version: str | None = None,
    ) -> None:
        s = get_settings()
        raw_domain = (domain or s.shopify_store_domain).strip()
        # Tolerate a pasted https:// prefix, trailing slash, or path — the
        # Admin API endpoint is built from a bare host.
        raw_domain = raw_domain.split("://", 1)[-1].strip("/").split("/", 1)[0]
        self.domain = raw_domain
        self.token = token or s.shopify_access_token
        self.api_version = api_version or s.shopify_api_version
        if not self.domain or not self.token:
            raise ShopifyError(
                "Shopify not configured: set SHOPIFY_STORE_DOMAIN and "
                "SHOPIFY_ACCESS_TOKEN."
            )
        self._endpoint = (
            f"https://{self.domain}/admin/api/{self.api_version}/graphql.json"
        )
        self._client = httpx.Client(
            headers={
                "X-Shopify-Access-Token": self.token,
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )

    # ── core request with basic throttle backoff ─────────────────
    def graphql(self, query: str, variables: dict | None = None) -> dict:
        for attempt in range(5):
            resp = self._client.post(
                self._endpoint, json={"query": query, "variables": variables or {}}
            )
            if resp.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                raise ShopifyError(f"GraphQL errors: {data['errors']}")
            return data["data"]
        raise ShopifyError("Shopify API throttled after retries (429).")

    @staticmethod
    def _check_user_errors(node: dict, key: str) -> None:
        errors = node.get("userErrors") or []
        if errors:
            raise ShopifyError(f"{key} userErrors: {errors}")

    # ── blog resolution ──────────────────────────────────────────
    def default_blog_id(self) -> str:
        configured = get_settings().shopify_blog_id
        if configured:
            return _as_gid(configured, "Blog")
        data = self.graphql(
            "query { blogs(first: 1) { nodes { id title } } }"
        )
        nodes = data["blogs"]["nodes"]
        if not nodes:
            raise ShopifyError("Store has no blogs; create one in Shopify admin.")
        return nodes[0]["id"]

    # ── reads for dedup / internal linking ───────────────────────
    def list_published(self, limit: int = 250) -> list[dict]:
        """Published articles as {id, title, handle, publishedAt}.

        Paginates rather than taking the first page: a store with more posts
        than one page would otherwise import a silent subset, and a dedup
        corpus that's quietly missing entries is worse than none — it reads
        as "no duplicate found".
        """
        query = """
        query($n: Int!, $after: String) {
          articles(first: $n, after: $after, query: "published_status:published") {
            nodes { id title handle publishedAt blog { handle } }
            pageInfo { hasNextPage endCursor }
          }
        }
        """
        out: list[dict] = []
        cursor: str | None = None
        while len(out) < limit:
            data = self.graphql(
                query, {"n": min(250, limit - len(out)), "after": cursor}
            )["articles"]
            nodes = data["nodes"]
            out.extend(nodes)
            info = data["pageInfo"]
            if not nodes or not info["hasNextPage"]:
                break
            cursor = info["endCursor"]
        return out[:limit]

    def list_link_targets(self, limit: int = 100) -> list[dict]:
        """Collections + pages usable as internal-link anchors: {title, url}.

        Collections (category names like "Laminate Flooring") and service/local
        pages ("Flooring in Langley", "Our Services") are what actually appear
        as phrases in article prose — individual product SKUs almost never do,
        so they're deliberately excluded. Junk targets (theme sitemap pages,
        the home page, brand sub-collections, encoding-glitched titles) are
        filtered out. Pages come first (highest-value local/service links),
        then collections.
        """
        query = """
        query($n: Int!) {
          collections(first: $n) { nodes { title handle } }
          pages(first: $n) { nodes { title handle } }
        }
        """
        data = self.graphql(query, {"n": limit})
        # Prefer the public storefront domain so links point at the live site
        # (e.g. drflooring.ca) rather than the *.myshopify.com URL.
        base = get_settings().store_link_base or f"https://{self.domain}"

        def _ok(title: str, handle: str) -> bool:
            t = (title or "").strip().lower()
            if not t or "�" in title:  # blank or encoding-glitched
                return False
            if t.startswith(("html sitemap", "brands -")):  # theme junk / brand SKUs
                return False
            if handle in {"frontpage"} or handle.startswith("avada-sitemap"):
                return False
            return True

        targets: list[dict] = []
        for pg in data["pages"]["nodes"]:
            if _ok(pg["title"], pg["handle"]):
                targets.append({"title": pg["title"], "url": f"{base}/pages/{pg['handle']}"})
        for col in data["collections"]["nodes"]:
            if _ok(col["title"], col["handle"]):
                targets.append(
                    {"title": col["title"], "url": f"{base}/collections/{col['handle']}"}
                )
        return targets

    # ── image upload ─────────────────────────────────────────────
    def upload_image(
        self, image_bytes: bytes, filename: str, mime_type: str = "image/png"
    ) -> dict:
        """Staged upload -> PUT bytes -> fileCreate. Returns {id, url, alt?}."""
        staged = self.graphql(
            """
            mutation($input: [StagedUploadInput!]!) {
              stagedUploadsCreate(input: $input) {
                stagedTargets { url resourceUrl parameters { name value } }
                userErrors { field message }
              }
            }
            """,
            {
                "input": [
                    {
                        "resource": "FILE",
                        "filename": filename,
                        "mimeType": mime_type,
                        "httpMethod": "POST",
                    }
                ]
            },
        )["stagedUploadsCreate"]
        self._check_user_errors(staged, "stagedUploadsCreate")
        target = staged["stagedTargets"][0]

        # POST the bytes to the staged target (S3/GCS presigned form).
        form = {p["name"]: p["value"] for p in target["parameters"]}
        files = {"file": (filename, image_bytes, mime_type)}
        upload_resp = httpx.post(target["url"], data=form, files=files, timeout=120.0)
        upload_resp.raise_for_status()

        created = self.graphql(
            """
            mutation($files: [FileCreateInput!]!) {
              fileCreate(files: $files) {
                files { id fileStatus alt
                  preview { image { url } } }
                userErrors { field message }
              }
            }
            """,
            {"files": [{"originalSource": target["resourceUrl"], "contentType": "IMAGE"}]},
        )["fileCreate"]
        self._check_user_errors(created, "fileCreate")
        node = created["files"][0]
        preview = (node.get("preview") or {}).get("image") or {}
        return {"id": node["id"], "url": preview.get("url")}

    # ── publish ──────────────────────────────────────────────────
    def create_article(
        self,
        *,
        title: str,
        body_html: str,
        summary: str | None = None,
        handle: str | None = None,
        seo_title: str | None = None,
        seo_description: str | None = None,
        image_file_id: str | None = None,
        blog_id: str | None = None,
        author: str = "Content Team",
        published: bool = True,
        dry_run: bool = False,
    ) -> PublishResult:
        blog = blog_id or self.default_blog_id()
        article: dict[str, Any] = {
            "blogId": blog,
            "title": title,
            "body": body_html,
            "author": {"name": author},
            "isPublished": published,
        }
        if handle:
            article["handle"] = handle
        if summary:
            article["summary"] = summary
        # ArticleCreateInput has no `seo` field (unlike products). Article meta
        # title/description are set via the conventional `global.title_tag` /
        # `global.description_tag` metafields, which themes read for <title>
        # and <meta name="description">.
        metafields: list[dict] = []
        if seo_title:
            metafields.append({
                "namespace": "global", "key": "title_tag",
                "type": "single_line_text_field", "value": seo_title,
            })
        if seo_description:
            metafields.append({
                "namespace": "global", "key": "description_tag",
                "type": "single_line_text_field", "value": seo_description,
            })
        if metafields:
            article["metafields"] = metafields
        if image_file_id:
            article["image"] = {"id": image_file_id}

        if dry_run:
            return PublishResult(
                article_id=None, handle=handle, url=None, dry_run=True,
                payload={"article": article},
            )

        data = self.graphql(
            """
            mutation($article: ArticleCreateInput!) {
              articleCreate(article: $article) {
                article { id handle }
                userErrors { field message }
              }
            }
            """,
            {"article": article},
        )["articleCreate"]
        self._check_user_errors(data, "articleCreate")
        node = data["article"]
        url = f"https://{self.domain}/blogs/news/{node['handle']}" if node else None
        return PublishResult(
            article_id=node["id"], handle=node["handle"], url=url
        )

    def close(self) -> None:
        self._client.close()


def _as_gid(value: str, resource: str) -> str:
    """Accept a numeric id or full GID; normalize to GID form."""
    value = str(value).strip()
    if value.startswith("gid://"):
        return value
    return f"gid://shopify/{resource}/{value}"
