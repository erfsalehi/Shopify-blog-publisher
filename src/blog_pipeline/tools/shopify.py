"""Shopify Admin GraphQL client.

Two groups of operations share one client, because they share one store:

**Blog publishing** — what the article pipeline needs:
  * default_blog_id   — resolve the store's first blog if none configured
  * list_published    — published article titles/handles for dedup + internal links
  * list_link_targets — products/pages/articles used as internal-link anchors
  * list_collection_cards / list_product_cards — catalogue entries with
    imagery, for the mid-article conversion cards
  * upload_image      — stagedUploadsCreate -> POST -> fileCreate (returns file id/url)
  * create_article    — articleCreate mutation, published immediately

**Catalogue writing** — what the product importer needs (see
`dashboard/product_import.py`):
  * find_product / create_product / update_product
  * add_product_media  — images fetched by Shopify from a source URL
  * upload_file        — a PDF into Shopify Files, staged from bytes we hold
  * set_metafields     — specs and document links as structured data
  * find_collection / create_collection / add_products_to_collection

The client raises ShopifyError on GraphQL userErrors so callers can mark the
row failed with a real reason. `dry_run` short-circuits the create mutations
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


def _usable_target(title: str, handle: str) -> bool:
    """Whether a collection/page is fit to show a reader.

    Junk targets (theme sitemap pages, the home page, brand sub-collections,
    encoding-glitched titles) are excluded — they read as broken in an article
    whether they're a text link or a card.
    """
    t = (title or "").strip().lower()
    if not t or "�" in title:  # blank or encoding-glitched
        return False
    if t.startswith(("html sitemap", "brands -")):  # theme junk / brand SKUs
        return False
    if handle in {"frontpage"} or handle.startswith("avada-sitemap"):
        return False
    # Brand sub-collections are named for the manufacturer, not the product
    # ("Canadian Flooring Casablanca"), so they match no article and read as
    # noise in a card row. The title check above catches the ones prefixed
    # "Brands -"; this catches the rest, which only the handle reveals.
    if handle.startswith("brands-"):
        return False
    return True


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

    def fetch_article(self, article_id: str) -> dict:
        """Full body + metadata for one post — the refresh agent's input.

        Deliberately not part of list_published: bodies are large, and pulling
        70 of them just to dedupe on titles would be wasteful. Read live
        rather than from our own draft_html, because imported posts never had
        a body here and any post may have been edited in Shopify admin since.
        """
        data = self.graphql(
            """
            query($id: ID!) {
              article(id: $id) {
                id title handle body summary publishedAt
              }
            }
            """,
            {"id": _as_gid(article_id, "Article")},
        )
        article = data.get("article")
        if not article:
            raise ShopifyError(f"Article {article_id} not found in Shopify.")
        return article

    def update_article(
        self,
        article_id: str,
        *,
        body_html: str | None = None,
        title: str | None = None,
        summary: str | None = None,
        seo_title: str | None = None,
        seo_description: str | None = None,
        dry_run: bool = False,
    ) -> PublishResult:
        """Overwrite a live post's fields in place.

        There is no hidden/staged variant of this: an already-published
        Shopify article has no draft revision, so this edits public content
        the moment it runs. Callers passing a body are expected to snapshot
        the previous one first (see db.ArticleRevision) — that is not
        reversible from Shopify's side. `isPublished` is deliberately never
        sent, so a refresh can't accidentally unpublish a page that's already
        ranking.

        `body_html` is optional so an SEO-only change can leave the prose
        alone. Sending the body back unchanged would still rewrite it, and
        Shopify does not return what it was given — it reformats — so a
        title-only edit would show up as a body edit and cost a revision for
        nothing.
        """
        article: dict[str, Any] = {}
        if body_html is not None:
            article["body"] = body_html
        if title:
            article["title"] = title
        if summary:
            article["summary"] = summary
        # ArticleUpdateInput has no `seo` field — same as ArticleCreateInput
        # (verified by introspection; the schema exposes only blogId/handle/
        # body/summary/isPublished/publishDate/templateSuffix/metafields/tags/
        # image/title/author/redirectNewHandle). Sending one makes Shopify
        # reject the whole variable, so the entire update fails rather than
        # just the SEO part. Meta title/description are the conventional
        # `global.title_tag` / `global.description_tag` metafields, which
        # themes read for <title> and <meta name="description">.
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

        if not article:
            raise ShopifyError("update_article called with nothing to change.")

        gid = _as_gid(article_id, "Article")
        if dry_run:
            return PublishResult(
                article_id=gid, handle=None, url=None, dry_run=True,
                payload={"id": gid, "article": article},
            )

        data = self.graphql(
            """
            mutation($id: ID!, $article: ArticleUpdateInput!) {
              articleUpdate(id: $id, article: $article) {
                article { id handle blog { handle } }
                userErrors { field message }
              }
            }
            """,
            {"id": gid, "article": article},
        )["articleUpdate"]
        self._check_user_errors(data, "articleUpdate")
        node = data.get("article") or {}
        handle = node.get("handle")
        blog_handle = (node.get("blog") or {}).get("handle")
        base = get_settings().store_link_base
        url = (
            f"{base}/blogs/{blog_handle}/{handle}"
            if handle and blog_handle and base
            else None
        )
        return PublishResult(article_id=node.get("id") or gid, handle=handle, url=url)

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

        targets: list[dict] = []
        for pg in data["pages"]["nodes"]:
            if _usable_target(pg["title"], pg["handle"]):
                targets.append({"title": pg["title"], "url": f"{base}/pages/{pg['handle']}"})
        for col in data["collections"]["nodes"]:
            if _usable_target(col["title"], col["handle"]):
                targets.append(
                    {"title": col["title"], "url": f"{base}/collections/{col['handle']}"}
                )
        return targets

    def list_collection_cards(self, limit: int = 100) -> list[dict]:
        """Collections rich enough to render as a card: {title, url, image}.

        Same catalogue as `list_link_targets`, read for a different job — a
        card shows a picture, so a collection with no image of its own falls
        back to its first product's photo, and one with neither is dropped
        rather than rendered as an empty box. Pages are excluded: a service
        page has no product imagery worth putting in a card.
        """
        data = self.graphql(
            """
            query($n: Int!) {
              collections(first: $n) {
                nodes {
                  title
                  handle
                  image { url }
                  products(first: 1) { nodes { featuredImage { url } } }
                }
              }
            }
            """,
            {"n": limit},
        )
        base = get_settings().store_link_base or f"https://{self.domain}"
        cards: list[dict] = []
        for col in data["collections"]["nodes"]:
            # Collection images are optional in Shopify and this store sets
            # none of them, so the first product's photo stands in. Without
            # the fallback the card row renders for nobody.
            image = (col.get("image") or {}).get("url")
            if not image:
                first = ((col.get("products") or {}).get("nodes") or [{}])[0]
                image = (first.get("featuredImage") or {}).get("url")
            if not image or not _usable_target(col["title"], col["handle"]):
                continue
            cards.append(
                {
                    "title": col["title"],
                    "url": f"{base}/collections/{col['handle']}",
                    "image": image,
                    "match_text": col["title"],
                }
            )
        return cards

    def list_product_cards(self, limit: int = 5000) -> list[dict]:
        """Buyable products as cards: {title, url, image, price, match_text}.

        Only active products that have a picture and are actually orderable —
        sending a reader mid-article to a sold-out SKU is worse than sending
        them nowhere. Products that don't track inventory are kept, since a
        zero there means "not counted", not "none left".

        **Paginates the whole catalogue.** The first version took one page of
        100 out of 2,908, so the matcher chose the most relevant product from
        3% of the store and a ceramics article got an SPC plank. Relevance
        can't beat a candidate list that never contained the right answer.

        `match_text` is what the article is matched against, and it is
        deliberately more than the title: these titles lead with the brand
        ("AquaFix SPC Plank - Aqua"), so the words a reader would search for
        live in the product's collections ("SPC", "Vinyl Flooring") and its
        useful tags. Import bookkeeping and brand tags are dropped — they
        appear on most of the catalogue and so distinguish nothing.
        """
        query = """
        query($n: Int!, $after: String) {
          products(first: $n, after: $after, query: "status:ACTIVE") {
            nodes {
              title
              handle
              tags
              totalInventory
              tracksInventory
              featuredImage { url }
              priceRangeV2 { minVariantPrice { amount currencyCode } }
              collections(first: 20) { nodes { title handle } }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
        """
        base = get_settings().store_link_base or f"https://{self.domain}"
        cards: list[dict] = []
        cursor: str | None = None
        while len(cards) < limit:
            data = self.graphql(query, {"n": 250, "after": cursor})["products"]
            for prod in data["nodes"]:
                image = (prod.get("featuredImage") or {}).get("url")
                if not image:
                    continue
                inventory = prod.get("totalInventory")
                if (
                    prod.get("tracksInventory")
                    and inventory is not None
                    and inventory <= 0
                ):
                    continue
                money = (prod.get("priceRangeV2") or {}).get("minVariantPrice") or {}
                cards.append(
                    {
                        "title": prod["title"],
                        "url": f"{base}/products/{prod['handle']}",
                        "image": image,
                        "price": _format_money(
                            money.get("amount"), money.get("currencyCode")
                        ),
                        "match_text": _match_text(prod),
                    }
                )
            info = data["pageInfo"]
            if not data["nodes"] or not info["hasNextPage"]:
                break
            cursor = info["endCursor"]
        return cards[:limit]

    # ── file upload ──────────────────────────────────────────────
    def _stage_upload(
        self, data: bytes, filename: str, mime_type: str, resource: str = "FILE"
    ) -> str:
        """Reserve a staged target, POST the bytes to it, return its
        resourceUrl — the handle `fileCreate` takes in place of a public URL.

        Split out because images and documents differ only in what they tell
        `fileCreate` afterwards; the upload itself is identical.
        """
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
                        "resource": resource,
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
        files = {"file": (filename, data, mime_type)}
        upload_resp = httpx.post(target["url"], data=form, files=files, timeout=120.0)
        upload_resp.raise_for_status()
        return target["resourceUrl"]

    def upload_image(
        self, image_bytes: bytes, filename: str, mime_type: str = "image/png"
    ) -> dict:
        """Staged upload -> POST bytes -> fileCreate. Returns {id, url, alt?}."""
        resource_url = self._stage_upload(image_bytes, filename, mime_type)

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
            {"files": [{"originalSource": resource_url, "contentType": "IMAGE"}]},
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

    # ── files that aren't images ─────────────────────────────────
    def upload_file(
        self,
        data: bytes,
        filename: str,
        mime_type: str = "application/pdf",
        alt: str | None = None,
        *,
        wait: bool = True,
    ) -> dict:
        """A document into Shopify Files. Returns {id, url}.

        Staged from bytes we already hold rather than handing Shopify the
        manufacturer's URL to fetch: the bytes are the ones whose text went
        into the description, so the file a customer downloads is provably
        the file the page describes. It also survives the supplier
        reorganising their site an hour later.

        Shopify processes an uploaded file asynchronously, so the CDN URL is
        usually absent from the `fileCreate` response. `wait` polls for it,
        because a download link is the entire point of uploading this.
        """
        resource_url = self._stage_upload(data, filename, mime_type)
        payload: dict[str, Any] = {
            "originalSource": resource_url,
            "contentType": "FILE",
        }
        if alt:
            payload["alt"] = alt[:512]
        created = self.graphql(
            """
            mutation($files: [FileCreateInput!]!) {
              fileCreate(files: $files) {
                files {
                  id fileStatus alt
                  ... on GenericFile { url }
                }
                userErrors { field message }
              }
            }
            """,
            {"files": [payload]},
        )["fileCreate"]
        self._check_user_errors(created, "fileCreate")
        node = created["files"][0]
        url = node.get("url")
        if not url and wait:
            url = self.wait_for_file_url(node["id"])
        return {"id": node["id"], "url": url}

    def wait_for_file_url(self, file_gid: str, attempts: int = 6) -> str | None:
        """Poll a file until Shopify finishes processing it and gives a URL.

        Bounded and allowed to give up: a file with no URL yet is still
        uploaded and still linked from the product's metafield, so the cost
        of giving up early is a missing download link on one product, not a
        lost file. Blocking a whole import on Shopify's queue would be worse.
        """
        for attempt in range(attempts):
            time.sleep(min(1.5 * (attempt + 1), 6.0))
            node = self.graphql(
                """
                query($id: ID!) {
                  node(id: $id) {
                    ... on GenericFile { id fileStatus url }
                  }
                }
                """,
                {"id": file_gid},
            ).get("node") or {}
            if node.get("url"):
                return node["url"]
            if str(node.get("fileStatus") or "").upper() == "FAILED":
                raise ShopifyError(f"Shopify failed to process file {file_gid}.")
        return None

    # ── products ─────────────────────────────────────────────────
    def find_product(self, handle: str) -> dict | None:
        """The product with this handle, or None.

        Checked before every create, which is what makes re-running an import
        safe: the second run finds what the first one made and leaves it
        alone instead of failing on a taken handle or creating a duplicate.
        """
        data = self.graphql(
            """
            query($q: String!) {
              products(first: 1, query: $q) {
                nodes { id handle title status onlineStoreUrl }
              }
            }
            """,
            {"q": f"handle:{handle}"},
        )
        nodes = (data.get("products") or {}).get("nodes") or []
        for node in nodes:
            if node.get("handle") == handle:
                return node
        return None

    def create_product(
        self,
        *,
        title: str,
        description_html: str,
        handle: str | None = None,
        vendor: str | None = None,
        product_type: str | None = None,
        tags: list[str] | None = None,
        seo_title: str | None = None,
        seo_description: str | None = None,
        status: str = "DRAFT",
        metafields: list[dict] | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Create one product. DRAFT unless told otherwise.

        No price and no variants: the importer reads manufacturer pages,
        which publish neither, and this store shows "call for price" on most
        of the catalogue anyway. Shopify creates a default variant on its own,
        which is where a price goes later if one is ever set.
        """
        product: dict[str, Any] = {
            "title": title,
            "descriptionHtml": description_html,
            "status": (status or "DRAFT").upper(),
        }
        if handle:
            product["handle"] = handle
        if vendor:
            product["vendor"] = vendor
        if product_type:
            product["productType"] = product_type
        if tags:
            product["tags"] = tags
        if seo_title or seo_description:
            # ProductCreateInput takes `seo` directly, unlike articles. Both
            # fields go together — see product_seo.py on Shopify replacing
            # this object wholesale rather than merging it.
            product["seo"] = {"title": seo_title, "description": seo_description}
        if metafields:
            product["metafields"] = metafields

        if dry_run:
            return {"dry_run": True, "product": product}

        data = self.graphql(
            """
            mutation($product: ProductCreateInput!) {
              productCreate(product: $product) {
                product { id handle title status onlineStoreUrl }
                userErrors { field message }
              }
            }
            """,
            {"product": product},
        )["productCreate"]
        self._check_user_errors(data, "productCreate")
        return data["product"]

    def update_product(
        self,
        product_gid: str,
        *,
        description_html: str | None = None,
        tags: list[str] | None = None,
        status: str | None = None,
    ) -> dict:
        """Change an existing product. Used by the cross-linking pass, which
        rewrites each description once its siblings exist."""
        product: dict[str, Any] = {"id": _as_gid(product_gid, "Product")}
        if description_html is not None:
            product["descriptionHtml"] = description_html
        if tags is not None:
            product["tags"] = tags
        if status is not None:
            product["status"] = status.upper()
        data = self.graphql(
            """
            mutation($input: ProductInput!) {
              productUpdate(input: $input) {
                product { id handle }
                userErrors { field message }
              }
            }
            """,
            {"input": product},
        )["productUpdate"]
        self._check_user_errors(data, "productUpdate")
        return data["product"]

    def add_product_media(
        self, product_gid: str, media: list[dict], *, dry_run: bool = False
    ) -> list[dict]:
        """Attach images to a product from their source URLs.

        Shopify fetches each URL itself and stores its own copy on the store's
        CDN — so this both saves the picture and avoids downloading megabytes
        of photography through this process to upload it straight back out.

        `media` is [{"originalSource": url, "alt": text}].

        **Returning without error does not mean the pictures arrived.**
        `mediaUserErrors` catches malformed input and nothing else: Shopify
        queues each URL and fetches it afterwards, so a CDN that refuses the
        fetch produces a perfectly successful mutation and a media object
        that turns `FAILED` a second later. Call `wait_for_media` on the ids
        returned here to find out what actually happened.
        """
        if not media:
            return []
        payload = [
            {
                "originalSource": item["originalSource"],
                "alt": (item.get("alt") or "")[:512],
                "mediaContentType": "IMAGE",
            }
            for item in media
        ]
        if dry_run:
            return payload
        data = self.graphql(
            """
            mutation($productId: ID!, $media: [CreateMediaInput!]!) {
              productCreateMedia(productId: $productId, media: $media) {
                media { ... on MediaImage { id status } }
                mediaUserErrors { field message }
              }
            }
            """,
            {"productId": _as_gid(product_gid, "Product"), "media": payload},
        )["productCreateMedia"]
        errors = data.get("mediaUserErrors") or []
        if errors:
            raise ShopifyError(f"productCreateMedia userErrors: {errors}")
        return data.get("media") or []

    def wait_for_media(
        self, media_ids: list[str], *, attempts: int = 6, pause: float = 1.0
    ) -> dict[str, str]:
        """Poll queued media until each is READY or FAILED. {id: status}.

        The other half of `add_product_media`. Ingestion is asynchronous, so
        the only way to learn that a manufacturer's CDN refused Shopify's
        fetch is to ask afterwards — and it is worth asking, because the
        alternative is a product recorded as having two photographs and
        showing none.

        Anything still PROCESSING when the attempts run out is reported as
        it stands rather than waited on further: this runs inside a request
        with a hard ceiling, and a slow image is not a failed one.
        """
        pending = [_as_gid(m, "MediaImage") for m in media_ids if m]
        if not pending:
            return {}
        seen: dict[str, str] = {}
        for attempt in range(attempts):
            data = self.graphql(
                """
                query($ids: [ID!]!) {
                  nodes(ids: $ids) {
                    ... on MediaImage { id status }
                  }
                }
                """,
                {"ids": pending},
            )
            for node in data.get("nodes") or []:
                if node and node.get("id"):
                    seen[node["id"]] = node.get("status") or "UNKNOWN"
            pending = [
                mid for mid in pending
                if seen.get(mid) in (None, "UPLOADED", "PROCESSING")
            ]
            if not pending:
                break
            if attempt < attempts - 1:
                time.sleep(pause)
        return seen

    def set_metafields(self, metafields: list[dict]) -> list[dict]:
        """Write metafields on any resource. Each entry needs ownerId,
        namespace, key, type and value.

        Specs and document links go here as well as into the description
        HTML: the description is what a customer reads, and the metafield is
        what a theme, an export, or a feed can read as data.
        """
        if not metafields:
            return []
        data = self.graphql(
            """
            mutation($metafields: [MetafieldsSetInput!]!) {
              metafieldsSet(metafields: $metafields) {
                metafields { id namespace key }
                userErrors { field message }
              }
            }
            """,
            {"metafields": metafields},
        )["metafieldsSet"]
        self._check_user_errors(data, "metafieldsSet")
        return data.get("metafields") or []

    # ── collections ──────────────────────────────────────────────
    def find_collection(self, handle: str) -> dict | None:
        data = self.graphql(
            """
            query($q: String!) {
              collections(first: 1, query: $q) {
                nodes { id handle title }
              }
            }
            """,
            {"q": f"handle:{handle}"},
        )
        nodes = (data.get("collections") or {}).get("nodes") or []
        for node in nodes:
            if node.get("handle") == handle:
                return node
        return None

    def create_collection(
        self,
        *,
        title: str,
        description_html: str = "",
        handle: str | None = None,
        seo_title: str | None = None,
        seo_description: str | None = None,
        product_gids: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict:
        """A manual collection holding exactly the products we put in it.

        Manual rather than a smart collection with a tag rule: the import
        knows precisely which products belong, and a rule would also sweep up
        anything else that later happens to carry the tag.
        """
        collection: dict[str, Any] = {
            "title": title,
            "descriptionHtml": description_html,
        }
        if handle:
            collection["handle"] = handle
        if seo_title or seo_description:
            collection["seo"] = {"title": seo_title, "description": seo_description}
        if product_gids:
            collection["products"] = product_gids

        if dry_run:
            return {"dry_run": True, "collection": collection}

        data = self.graphql(
            """
            mutation($input: CollectionInput!) {
              collectionCreate(input: $input) {
                collection { id handle title }
                userErrors { field message }
              }
            }
            """,
            {"input": collection},
        )["collectionCreate"]
        self._check_user_errors(data, "collectionCreate")
        return data["collection"]

    def add_products_to_collection(
        self, collection_gid: str, product_gids: list[str]
    ) -> None:
        """Add products to an existing collection, in batches.

        Batched because `collectionAddProducts` is one of the more expensive
        mutations in the Admin API's cost model, and a 60-product collection
        sent as one call is the kind of thing that gets throttled.
        """
        gid = _as_gid(collection_gid, "Collection")
        for start in range(0, len(product_gids), 25):
            batch = product_gids[start:start + 25]
            if not batch:
                continue
            data = self.graphql(
                """
                mutation($id: ID!, $productIds: [ID!]!) {
                  collectionAddProducts(id: $id, productIds: $productIds) {
                    collection { id }
                    userErrors { field message }
                  }
                }
                """,
                {"id": gid, "productIds": batch},
            )["collectionAddProducts"]
            self._check_user_errors(data, "collectionAddProducts")

    # ── links back to the admin ──────────────────────────────────
    def admin_url(self, gid: str) -> str:
        """Where the owner goes to look at what was created."""
        numeric = str(gid).rsplit("/", 1)[-1]
        kind = str(gid).split("/")[-2].lower() if "/" in str(gid) else "product"
        return f"https://{self.domain}/admin/{kind}s/{numeric}"


    def close(self) -> None:
        self._client.close()


# Tags that sit on most of the catalogue and so separate nothing: import
# bookkeeping, the blanket "Brands" tag, and the location-marketing tags
# ("Best Laminate Flooring Langley"), which otherwise let any product match
# any article that mentions a place.
_JUNK_TAG_PREFIXES = (
    "import_", "validate-", "joined-", "brands", "best ", "wood and laminate",
)
_JUNK_COLLECTION_TITLES = {"home page", "all products"}


def _match_text(product: dict) -> str:
    """The words a product should be findable by.

    The title alone is not enough: it leads with the brand, so "AquaFix SPC
    Plank - Aqua" never matches an article about vinyl. Its collections say
    what it actually is.
    """
    parts = [product.get("title") or ""]
    for col in ((product.get("collections") or {}).get("nodes") or []):
        handle, title = col.get("handle") or "", col.get("title") or ""
        if handle.startswith("brands-") or title.lower() in _JUNK_COLLECTION_TITLES:
            continue
        parts.append(title)
    for tag in (product.get("tags") or []):
        if not tag.lower().startswith(_JUNK_TAG_PREFIXES):
            parts.append(tag)
    return " ".join(p for p in parts if p)


_CURRENCY_SYMBOLS = {"USD": "$", "CAD": "$", "AUD": "$", "EUR": "€", "GBP": "£"}


def _format_money(amount: str | None, currency: str | None) -> str:
    """A price a reader recognises, or "" when the store didn't give us one."""
    if amount in (None, ""):
        return ""
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return ""
    symbol = _CURRENCY_SYMBOLS.get((currency or "").upper())
    return f"{symbol}{value:,.2f}" if symbol else f"{value:,.2f} {currency or ''}".strip()


def _as_gid(value: str, resource: str) -> str:
    """Accept a numeric id or full GID; normalize to GID form."""
    value = str(value).strip()
    if value.startswith("gid://"):
        return value
    return f"gid://shopify/{resource}/{value}"
