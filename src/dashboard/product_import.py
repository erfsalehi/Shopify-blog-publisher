"""Turning a pasted collection URL into products in the store.

One run, four stages, each one resumable:

    discover → products → collection → linking → done

**Why stages and not a function.** Importing 40 products is 40 page fetches,
a hundred PDF downloads, 40 LLM calls and several hundred Shopify mutations —
minutes of work. A Vercel function is killed at 60 seconds and this app runs
on Vercel. So `advance()` does a bounded slice of whatever the run needs next,
writes its progress, and returns; whoever calls it again — the run page
polling, the cron job, the owner clicking Continue — carries on from there.
Nothing is held in memory between passes, which is also what makes a crash
mid-import cost one product rather than the run.

**Why the linking stage exists separately.** Every product in a range should
link to its siblings, with a picture, and no product can link to a sibling
that doesn't exist yet. So the descriptions are written twice: once at
creation, and once more at the end when every product has an ID and a URL.

**What it refuses to do.** Prices are never set — manufacturer pages don't
publish retail prices and inventing one is not an option. And an existing
product with the same handle is left completely alone: the second run of an
import finds what the first one made rather than duplicating or overwriting
it.

**What it does do, loudly.** Products are created Active and published to
every sales channel, which means an import is live to customers as it runs.
That is the owner's setting and their decision — the previous default was
Draft, and it meant every import finished with a second, manual pass in
Shopify admin that was easy to forget and left the catalogue half-published.
The dry run is what stands between a bad extraction and the storefront now,
so it earns its minutes.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from dashboard import product_copy, product_docs, store
from dashboard.competitors import FetchError
from dashboard.config import is_serverless
from dashboard.db import get_session
from dashboard.manufacturer import (
    SourceDoc,
    SourceImage,
    SourceProduct,
    client as source_client,
    discover_collection,
    fetch_product,
)
from dashboard.models import ImportProduct, ImportProductStatus, ImportRun, ImportStage

log = logging.getLogger(__name__)

#: Wall clock one `advance()` may spend before returning, whatever stage it's
#: in. Under Vercel's 60s ceiling with room to finish the product in hand and
#: write it; generous locally, where the only cost of a long pass is a slower
#: page refresh.
PASS_BUDGET_SECONDS = 40.0
LOCAL_PASS_BUDGET_SECONDS = 240.0

#: Fallback for the number of siblings linked from each product page. The
#: real value is `store.IMPORT_RELATED_LIMIT`; this is what applies when the
#: settings table can't be reached. Every product in a range should link to
#: every other one — the cap exists because Shopify limits a description to
#: 65,535 characters, not because eight is the right number.
RELATED_LIMIT = 60


def _related_limit() -> int:
    try:
        return int(store.get(store.IMPORT_RELATED_LIMIT))
    except Exception:  # noqa: BLE001 - a missing setting must not stop linking
        return RELATED_LIMIT

METAFIELD_NAMESPACE = "custom"


class ImportRunError(RuntimeError):
    """A run-level failure: nothing about this collection can proceed."""


@dataclass
class PassResult:
    run_id: int
    stage: str
    done: bool
    handled: int = 0
    message: str = ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _budget() -> float:
    return PASS_BUDGET_SECONDS if is_serverless() else LOCAL_PASS_BUDGET_SECONDS


# ── Starting a run ───────────────────────────────────────────────────


def start_run(
    source_url: str,
    *,
    dry_run: bool = False,
    collection_title: str | None = None,
    vendor: str | None = None,
    max_products: int | None = None,
    publish_status: str | None = None,
    make_collection: bool = True,
    link_products: bool = True,
    collection_mode: str = "new",
    build_page: bool = True,
) -> int:
    """Record the request and return its run id. Does no fetching.

    Deliberately does no work: the caller is an HTTP request handler, and the
    first thing the owner should see is a run page they can watch, not a
    spinner on the form for however long a supplier's site takes to answer.
    """
    options = {
        "max_products": int(max_products or store.get(store.IMPORT_MAX_PRODUCTS)),
        "publish_status": (
            publish_status or store.get(store.IMPORT_PUBLISH_STATUS) or "DRAFT"
        ).upper(),
        "make_collection": bool(make_collection),
        "link_products": bool(link_products),
        # "new" | "update". Asked rather than inferred: a range imported
        # before its collection page was ever built looks new from the
        # store's side, and guessing wrong creates a second page for a range
        # that already has one.
        "collection_mode": (
            "update" if str(collection_mode).lower() == "update" else "new"
        ),
        "build_page": bool(build_page),
        "all_channels": bool(store.get(store.IMPORT_ALL_CHANNELS)),
        "max_images": int(store.get(store.IMPORT_MAX_IMAGES)),
        "max_docs": int(store.get(store.IMPORT_MAX_DOCS)),
        "batch": int(store.get(store.IMPORT_BATCH)),
        "source_tag": str(store.get(store.IMPORT_TAG_PREFIX) or "").strip(),
    }
    with get_session() as session:
        run = ImportRun(
            source_url=source_url.strip(),
            dry_run=bool(dry_run),
            collection_title=(collection_title or "").strip() or None,
            vendor=(vendor or "").strip() or None,
            options_json=json.dumps(options),
            stage=ImportStage.discover.value,
        )
        run.note(
            f"Queued {'a dry run of ' if dry_run else ''}{source_url.strip()}"
        )
        session.add(run)
        session.flush()
        return run.id


# ── The pass ─────────────────────────────────────────────────────────


def advance(run_id: int) -> PassResult:
    """Do the next bounded slice of work for this run.

    Safe to call on a finished run (returns immediately) and safe to call
    twice concurrently in the sense that matters — the work is idempotent per
    product, and the handle check before every create means the worst outcome
    of a double call is wasted fetches, not duplicate products.
    """
    with get_session() as session:
        run = session.get(ImportRun, run_id)
        if run is None:
            raise ImportRunError(f"No import run {run_id}.")
        stage = run.stage
        if not run.is_active:
            return PassResult(run_id, stage, done=True, message="finished")

    try:
        if stage == ImportStage.discover.value:
            return _discover(run_id)
        if stage == ImportStage.products.value:
            return _products(run_id)
        if stage == ImportStage.collection.value:
            return _collection(run_id)
        if stage == ImportStage.linking.value:
            return _linking(run_id)
    except ImportRunError as e:
        _fail(run_id, str(e))
        return PassResult(run_id, ImportStage.failed.value, done=True, message=str(e))
    except Exception as e:  # noqa: BLE001 - a run-level crash must be legible
        log.exception("import run %s crashed in %s", run_id, stage)
        message = f"{type(e).__name__}: {e}"
        _fail(run_id, message)
        return PassResult(run_id, ImportStage.failed.value, done=True, message=message)

    return PassResult(run_id, stage, done=True, message="nothing to do")


def _fail(run_id: int, message: str) -> None:
    with get_session() as session:
        run = session.get(ImportRun, run_id)
        if run is None:
            return
        run.stage = ImportStage.failed.value
        run.error = message[:2000]
        run.finished_at = _now()
        run.note(f"Failed: {message[:300]}")


def _set_stage(session, run: ImportRun, stage: ImportStage, message: str) -> None:
    run.stage = stage.value
    run.note(message)
    if stage is ImportStage.done:
        run.finished_at = _now()


# ── Stage 1: discover ────────────────────────────────────────────────


def _discover(run_id: int) -> PassResult:
    with get_session() as session:
        run = session.get(ImportRun, run_id)
        source_url, options = run.source_url, run.options
        dry_run = run.dry_run
        forced_title, forced_vendor = run.collection_title, run.vendor

    try:
        collection = discover_collection(
            source_url, max_products=int(options.get("max_products", 60))
        )
    except FetchError as e:
        raise ImportRunError(str(e)) from e

    with get_session() as session:
        run = session.get(ImportRun, run_id)
        run.source_base = collection.base
        run.collection_title = forced_title or collection.title or "Imported collection"
        run.collection_handle = product_copy.slugify(
            run.collection_title, fallback="imported-collection"
        )
        existing = {
            row.source_url
            for row in session.query(ImportProduct.source_url)
            .filter(ImportProduct.run_id == run_id)
            .all()
        }
        added = 0
        for position, url in enumerate(collection.product_urls, start=1):
            if url in existing:
                continue
            seed = collection.seeds.get(url)
            if not forced_vendor and seed and seed.vendor and not run.vendor:
                run.vendor = seed.vendor
            session.add(
                ImportProduct(
                    run_id=run_id,
                    position=position,
                    source_url=url,
                    source_handle=(seed.handle if seed else None),
                    title=(seed.title if seed else None),
                    extracted_json=json.dumps(seed.as_dict()) if seed else "{}",
                    status=ImportProductStatus.pending.value,
                )
            )
            added += 1

        options["collection_description"] = (collection.description or "")[:2000]
        options["platform"] = collection.platform
        run.options_json = json.dumps(options)
        run.note(
            f"Found {added} products on {collection.platform} source "
            f"({collection.pages} pages read)."
            + (" Dry run — nothing will be created." if dry_run else "")
        )
        _set_stage(session, run, ImportStage.products, "Reading products.")
        return PassResult(run_id, ImportStage.products.value, done=False, handled=added)


# ── Stage 2: one product at a time ───────────────────────────────────


def _products(run_id: int) -> PassResult:
    deadline = time.monotonic() + _budget()
    with get_session() as session:
        run = session.get(ImportRun, run_id)
        options, dry_run = run.options, run.dry_run
        base, vendor = run.source_base, run.vendor
        collection_title = run.collection_title
        pending = (
            session.query(ImportProduct.id)
            .filter(
                ImportProduct.run_id == run_id,
                ImportProduct.status == ImportProductStatus.pending.value,
            )
            .order_by(ImportProduct.position)
            .limit(int(options.get("batch", 3)))
            .all()
        )
        ids = [row.id for row in pending]

    if not ids:
        with get_session() as session:
            run = session.get(ImportRun, run_id)
            counts = _counts(session, run_id)
            next_stage = (
                ImportStage.collection
                if options.get("make_collection", True)
                else ImportStage.linking
            )
            _set_stage(
                session, run, next_stage,
                f"{counts['created']} created, {counts['skipped']} already existed, "
                f"{counts['failed']} failed.",
            )
        return PassResult(run_id, next_stage.value, done=False)

    client = _shopify_client() if not dry_run else None
    http = source_client()
    handled = 0
    try:
        for product_id in ids:
            _one_product(
                product_id,
                run_id=run_id,
                base=base,
                vendor=vendor,
                collection_title=collection_title,
                collection_description=options.get("collection_description"),
                options=options,
                dry_run=dry_run,
                client=client,
                http=http,
            )
            handled += 1
            if time.monotonic() > deadline:
                break
    finally:
        http.close()

    with get_session() as session:
        run = session.get(ImportRun, run_id)
        run.updated_at = _now()
        remaining = (
            session.query(ImportProduct)
            .filter(
                ImportProduct.run_id == run_id,
                ImportProduct.status == ImportProductStatus.pending.value,
            )
            .count()
        )
    return PassResult(
        run_id, ImportStage.products.value, done=False, handled=handled,
        message=f"{remaining} to go",
    )


def _range_shape(run_id: int, *, size: str, kind: str) -> tuple[str, str]:
    """Settle the range's size and product type once, then reuse them.

    Both describe the range rather than the item — every product in a tile
    series is the same size and the same kind of tile — and both were being
    asked per product, which is how one range came to ship under three
    different naming patterns in a single run.

    First non-empty answer wins and is kept on the run. That leaves one
    residual case honestly: if the first product yields nothing and a later
    one does, the first keeps the shorter name. One product off-pattern is a
    great deal better than nine, and the alternative — holding every create
    until the whole range has been read — would cost a pass per product and
    still guess for a range where no product ever answers.
    """
    with get_session() as session:
        run = session.get(ImportRun, run_id)
        options = run.options
        settled_size = str(options.get("range_size") or "")
        settled_kind = str(options.get("range_product_type") or "")

        changed = False
        if not settled_size and size:
            options["range_size"] = settled_size = size
            changed = True
        if not settled_kind and kind:
            options["range_product_type"] = settled_kind = kind
            changed = True
        if changed:
            run.options_json = json.dumps(options)

    return settled_size or size, settled_kind or kind


def _one_product(
    product_id: int,
    *,
    run_id: int,
    base: str,
    vendor: str | None,
    collection_title: str | None,
    collection_description: str | None,
    options: dict,
    dry_run: bool,
    client,
    http,
) -> None:
    """Scrape, read, write and create one product. Never raises.

    A product that fails is marked failed with its reason and the run carries
    on. One supplier page returning a 500 should not decide the fate of the
    other thirty-nine.
    """
    with get_session() as session:
        row = session.get(ImportProduct, product_id)
        source_url = row.source_url
        seed_data = row.extracted

    try:
        seed = _seed_from(seed_data, source_url)
        source = fetch_product(source_url, base, seed=seed, http=http)
        source.images = source.images[: int(options.get("max_images", 8))]

        max_docs = int(options.get("max_docs", 4))
        if max_docs and source.docs:
            product_docs.read_docs(
                source.docs, http=http, limit=max_docs, keep_data=not dry_run
            )
        elif not max_docs:
            source.docs = []

        copy, model_used = product_copy.write_copy(
            source,
            collection_title=collection_title,
            collection_description=collection_description,
            vendor=vendor or source.vendor,
        )

        # The store's naming standard, applied here rather than left to the
        # model: brand, range, type, colour. Done before the row is written
        # so a dry run shows the name the product would actually get.
        # The manufacturer's own title is the better source for what
        # separates this item from its siblings, and the model is the
        # fallback rather than the other way round. Asked for it directly it
        # returned nothing for twelve products out of fourteen, and for the
        # one it answered it said "Onix" — dropping the finish, which is
        # half the discriminator in a range where "Onix Bevel Gloss" and
        # "Onix Diamond Gloss" are different products.
        # Type and size describe the RANGE, not the item: every product in
        # "3D Bars" is the same 5"x10" glazed ceramic wall tile. Asked per
        # product they came back three ways — the full type for four of
        # thirteen and nothing for the rest, the size as 5"x10" once, 5" x
        # 10" once and absent otherwise — so one range shipped under three
        # different naming patterns. They are settled once and reused.
        size = product_copy.derive_size(source.title, source.specs) or copy.size
        product_type = copy.product_type or source.product_type or ""
        size, product_type = _range_shape(run_id, size=size, kind=product_type)
        copy.size, copy.product_type = size, product_type or copy.product_type

        variant = product_copy.derive_variant(
            source.title, collection=collection_title, size=size,
        ) or copy.color
        copy.color = variant
        copy.title = product_copy.compose_title(
            brand=vendor or source.vendor,
            collection=collection_title,
            product_type=product_type,
            size=size,
            color=variant,
            fallback=copy.title or source.title,
        )

        with get_session() as session:
            row = session.get(ImportProduct, product_id)
            row.title = copy.title
            row.extracted_json = json.dumps(source.as_dict())
            row.generated_json = json.dumps(
                {**copy.model_dump(), "model": model_used}
            )

        if dry_run:
            _finish_product(
                product_id, ImportProductStatus.prepared,
                note=f"prepared ({len(source.images)} images, "
                     f"{len([d for d in source.docs if d.text])} docs read)",
            )
            return

        _create_in_shopify(
            product_id,
            run_id=run_id,
            source=source,
            copy=copy,
            vendor=vendor or source.vendor,
            collection_title=collection_title or "",
            options=options,
            client=client,
        )
    except Exception as e:  # noqa: BLE001 - one product's failure, recorded
        log.warning("import product %s failed: %s", product_id, e, exc_info=True)
        _finish_product(
            product_id, ImportProductStatus.failed,
            error=f"{type(e).__name__}: {e}",
        )


def _seed_from(data: dict, source_url: str) -> SourceProduct:
    """Rebuild the discovery seed off the row, so a resumed run doesn't
    re-fetch what the collection feed already gave us."""
    if not data:
        return SourceProduct(source_url=source_url)
    seed = SourceProduct(
        source_url=data.get("source_url") or source_url,
        handle=data.get("handle") or "",
        title=data.get("title") or "",
        description_html=data.get("description_html") or "",
        description_text=data.get("description_text") or "",
        vendor=data.get("vendor"),
        sku=data.get("sku"),
        product_type=data.get("product_type"),
        tags=list(data.get("tags") or []),
        specs=dict(data.get("specs") or {}),
        options=dict(data.get("options") or {}),
        sources=dict(data.get("sources") or {}),
    )
    seed.images = [
        SourceImage(
            url=i.get("url", ""), alt=i.get("alt"), position=i.get("position", 0)
        )
        for i in (data.get("images") or [])
        if i.get("url")
    ]
    seed.docs = [
        SourceDoc(
            url=d.get("url", ""), title=d.get("title"), kind=d.get("kind", "other"),
        )
        for d in (data.get("docs") or [])
        if d.get("url")
    ]
    return seed


def _finish_product(
    product_id: int,
    status: ImportProductStatus,
    *,
    error: str | None = None,
    note: str | None = None,
    **fields,
) -> None:
    with get_session() as session:
        row = session.get(ImportProduct, product_id)
        if row is None:
            return
        row.status = status.value
        row.error = error[:2000] if error else None
        for key, value in fields.items():
            setattr(row, key, value)
        run = session.get(ImportRun, row.run_id)
        if run is not None:
            label = row.title or row.source_url
            if status is ImportProductStatus.failed:
                run.note(f"✗ {label}: {(error or 'failed')[:200]}")
            else:
                run.note(f"✓ {label} — {note or status.value}")


# ── Creating it in Shopify ───────────────────────────────────────────


def _shopify_client():
    from blog_pipeline.tools.shopify import ShopifyClient

    return ShopifyClient()


def _create_in_shopify(
    product_id: int,
    *,
    run_id: int,
    source: SourceProduct,
    copy: product_copy.ProductCopy,
    vendor: str | None,
    collection_title: str,
    options: dict,
    client,
) -> None:
    from blog_pipeline.tools.shopify import ShopifyError

    handle = product_copy.slugify(copy.title, fallback=source.handle or "product")
    existing = client.find_product(handle)
    if existing:
        _finish_product(
            product_id, ImportProductStatus.skipped,
            note="already in the store, left untouched",
            product_gid=existing["id"], handle=handle,
            admin_url=client.admin_url(existing["id"]),
            online_url=existing.get("onlineStoreUrl"),
        )
        return

    tags = product_copy.clean_tags(
        _required_tags(
            product_type=copy.product_type or source.product_type,
            vendor=vendor or source.vendor,
            collection_title=collection_title,
            source_tag=options.get("source_tag"),
        )
        + list(copy.tags)
    )

    locale = product_copy.locale_text()
    brand = product_copy.brand_blurb_text()
    banner = product_copy.banner_text()
    body = product_copy.render_description(
        copy, docs=source.docs, doc_urls={}, source_url=source.source_url,
        locale=locale, brand=brand, collection_title=collection_title,
        banner=banner,
    )

    created = client.create_product(
        title=copy.title,
        description_html=body,
        handle=handle,
        vendor=vendor,
        product_type=copy.product_type or source.product_type,
        tags=tags,
        seo_title=copy.seo_title,
        seo_description=copy.seo_description,
        status=options.get("publish_status", "DRAFT"),
    )
    product_gid = created["id"]

    # Asked for rather than assumed. Every channel has its own "automatically
    # publish new products" setting, so an import that never mentions
    # channels reaches whichever ones happen to have it on — which looks
    # identical to working right up until a store has one switched off.
    # Safe on a draft: the status still decides whether anyone can see it.
    if options.get("all_channels", True):
        try:
            client.publish_to_all_channels(product_gid)
        except ShopifyError as exc:
            # Not fatal. The product exists and is correct; it is a
            # permission or a channel problem, and failing the whole import
            # over it would throw away the work that did succeed.
            log.warning("could not publish %s to all channels: %s", product_gid, exc)

    images_saved = _attach_images(client, product_gid, source, copy)
    doc_urls, docs_saved = _attach_docs(client, product_gid, source)

    # The description is rewritten once the documents have store URLs — the
    # first version was written before they existed, and a Downloads section
    # that links the manufacturer's site instead of ours is the thing this
    # whole exercise is meant to avoid.
    if doc_urls:
        body = product_copy.render_description(
            copy, docs=source.docs, doc_urls=doc_urls,
            source_url=source.source_url, locale=locale, brand=brand,
            collection_title=collection_title, banner=banner,
        )
        client.update_product(product_gid, description_html=body)

    try:
        client.set_metafields(_metafields(product_gid, source, copy, doc_urls))
    except ShopifyError as e:
        # Metafields are the machine-readable copy of what's already in the
        # description. Losing them costs a theme feature, not the product.
        log.info("metafields for %s failed: %s", product_gid, e)

    # Where each document ended up, kept on the row: the cross-linking pass
    # rebuilds this description from scratch and would otherwise drop the
    # Downloads section it can no longer find the URLs for.
    with get_session() as session:
        row = session.get(ImportProduct, product_id)
        row.generated_json = json.dumps({**row.generated, "doc_urls": doc_urls})

    _finish_product(
        product_id, ImportProductStatus.created,
        note=f"{images_saved} images, {docs_saved} documents",
        product_gid=product_gid,
        handle=created.get("handle") or handle,
        admin_url=client.admin_url(product_gid),
        # Only what Shopify reports: a draft has no storefront URL, and
        # showing a link that 404s until someone publishes would be a lie the
        # run page tells about its own work.
        online_url=created.get("onlineStoreUrl"),
        images_saved=images_saved,
        docs_saved=docs_saved,
    )


def _attach_images(client, product_gid: str, source: SourceProduct, copy) -> int:
    """Images onto the product, with a fallback for a CDN that blocks Shopify.

    First choice is handing Shopify the source URL and letting it fetch — no
    bytes move through this process at all. Some manufacturer CDNs refuse
    that fetch (hotlink protection, a WAF), and the mutation reports it; the
    fallback downloads each image here, where the request looks like the same
    browser that read the product page, and uploads the bytes instead.
    """
    from blog_pipeline.tools.shopify import ShopifyError

    if not source.images:
        return 0
    alts = copy.image_alts or []
    media = [
        {
            "originalSource": image.url,
            "alt": (alts[index] if index < len(alts) else copy.title),
        }
        for index, image in enumerate(source.images)
    ]
    # What Shopify accepted is not what Shopify fetched. The mutation
    # queues each URL and returns immediately, so the only honest count
    # comes from asking afterwards which media went READY and which went
    # FAILED — a CDN with hotlink protection produces a clean mutation and
    # no picture, which is how a product came to be recorded as having two
    # photographs and to show none.
    retry: list[int] = []
    try:
        accepted = client.add_product_media(product_gid, media)
        statuses = client.wait_for_media([m.get("id") for m in accepted])
        for index, entry in enumerate(accepted):
            if statuses.get(entry.get("id")) == "FAILED":
                retry.append(index)
        # Anything Shopify never acknowledged is retried too: fewer media
        # back than sent means the rest were dropped, not queued.
        retry.extend(range(len(accepted), len(media)))
        if not retry:
            return len(media)
        log.info(
            "%d of %d images failed Shopify's own fetch for %s; uploading bytes",
            len(retry), len(media), product_gid,
        )
    except ShopifyError as e:
        log.info("media by URL failed for %s (%s); uploading bytes", product_gid, e)
        retry = list(range(len(source.images)))

    saved = len(media) - len(retry)
    http = source_client()
    try:
        for index, image in enumerate(source.images):
            if index not in retry:
                continue
            try:
                resp = http.get(image.url)
                resp.raise_for_status()
                mime = resp.headers.get("content-type", "image/jpeg").split(";")[0]
                filename = product_docs.filename_for(
                    SourceDoc(url=image.url, title=f"{copy.title} {index + 1}")
                )
                uploaded = client.upload_image(resp.content, filename, mime)
                if uploaded.get("url"):
                    client.add_product_media(
                        product_gid,
                        [{
                            "originalSource": uploaded["url"],
                            "alt": alts[index] if index < len(alts) else copy.title,
                        }],
                    )
                    saved += 1
            except Exception as e:  # noqa: BLE001 - one image is not the product
                log.info("image %s failed: %s", image.url, e)
    finally:
        http.close()
    return saved


def _required_tags(
    *, product_type: str | None, vendor: str | None,
    collection_title: str, source_tag: str | None,
) -> list[str]:
    """The tags the store needs, before the ones the model thought of.

    Order is the point. `clean_tags` caps a product at `MAX_TAGS`, and the
    model routinely proposes fifteen; appending these afterwards would let a
    talkative description push the brand off the end of the list. These are
    what the storefront is built on, so they go first and the model's
    suggestions fill whatever room is left.

    Brand and collection together are what make a smart collection possible
    — a page defined as "these two tags" needs both present on every product
    in the range, every time, not usually.
    """
    wanted = [collection_title, vendor, product_type, source_tag]
    return [str(t).strip() for t in wanted if t and str(t).strip()]


def _attach_docs(client, product_gid: str, source: SourceProduct) -> tuple[dict, int]:
    """Upload the PDFs we downloaded. Returns {source_url: store_url}, count.

    Only documents whose bytes we actually hold are uploaded — a doc that
    failed to download has nothing to upload, and pointing the store at the
    manufacturer's URL instead would be a link that breaks the day they
    redesign.
    """
    doc_urls: dict[str, str] = {}
    saved = 0
    #: Uploaded, but Shopify hasn't finished processing it into a URL yet.
    awaiting: list[tuple[str, str]] = []

    for doc in source.docs:
        if not doc.data:
            continue
        try:
            uploaded = client.upload_file(
                doc.data,
                product_docs.filename_for(doc),
                mime_type="application/pdf",
                alt=f"{doc.kind}: {doc.title or ''}".strip(": "),
                # Every upload first, then wait for the URLs — waiting on each
                # one in turn would serialise four independent uploads and
                # spend most of a 60-second pass asleep.
                wait=False,
            )
            saved += 1
            if uploaded.get("url"):
                doc_urls[doc.url] = uploaded["url"]
            elif uploaded.get("id"):
                awaiting.append((doc.url, uploaded["id"]))
        except Exception as e:  # noqa: BLE001 - a failed doc is not the product
            log.info("doc upload failed for %s: %s", doc.url, e)
        finally:
            # The bytes have done their job twice over — read, then uploaded.
            doc.data = None

    for source_url, file_gid in awaiting:
        try:
            url = client.wait_for_file_url(file_gid, attempts=3)
        except Exception as e:  # noqa: BLE001 - no URL is a missing link, not a failure
            log.info("file %s never got a URL: %s", file_gid, e)
            continue
        if url:
            doc_urls[source_url] = url
    return doc_urls, saved


def _metafields(product_gid: str, source: SourceProduct, copy, doc_urls: dict) -> list[dict]:
    """Specs, documents and provenance as structured data.

    The description is for people; this is for the theme, the product feed,
    and whoever has to answer "where did this product come from" later.
    """
    def field(key: str, type_: str, value) -> dict:
        return {
            "ownerId": product_gid,
            "namespace": METAFIELD_NAMESPACE,
            "key": key,
            "type": type_,
            "value": value if isinstance(value, str) else json.dumps(value),
        }

    fields = [field("source_url", "url", source.source_url)]
    if copy.specs:
        fields.append(
            field("specifications", "json",
                  [{"name": s.name, "value": s.value} for s in copy.specs])
        )
    documents = [
        {"title": d.title or d.kind, "kind": d.kind, "url": doc_urls[d.url]}
        for d in source.docs
        if d.url in doc_urls
    ]
    if documents:
        fields.append(field("documents", "json", documents))
    if copy.faqs:
        # Also as data, not only as the JSON-LD in the description body:
        # Shopify may strip a <script> tag from a product description
        # depending on the theme and the field's sanitising, and a theme that
        # renders the FAQ from here gets proper markup either way.
        fields.append(
            field("faq", "json",
                  [{"question": f.question, "answer": f.answer} for f in copy.faqs])
        )
    return fields


def _product_path(handle: str | None) -> str | None:
    """Where a product will live on our storefront, as a root-relative path.

    Relative on purpose. These links are written into other products'
    descriptions on the same store, so they need no domain — and a draft
    product has no `onlineStoreUrl` from Shopify at all, which would
    otherwise leave the whole range unlinked until someone published it.
    """
    return f"/products/{handle}" if handle else None


# ── Stage 3: the collection ──────────────────────────────────────────


def _collection(run_id: int) -> PassResult:
    with get_session() as session:
        run = session.get(ImportRun, run_id)
        options, dry_run = run.options, run.dry_run
        # The range name is what the products are tagged with and what the
        # "more from this range" heading reads. What goes on the shelf is
        # the store's own standard — brand, range, "Collection" — so the two
        # are composed separately rather than one being reused as the other.
        range_name = run.collection_title or "Imported collection"
        title = product_copy.compose_collection_title(
            brand=run.vendor, collection=range_name,
        ) or range_name
        handle = product_copy.slugify(title, fallback="imported-collection")
        description = options.get("collection_description")
        rows = (
            session.query(ImportProduct)
            .filter(
                ImportProduct.run_id == run_id,
                ImportProduct.status.in_([
                    ImportProductStatus.created.value,
                    ImportProductStatus.skipped.value,
                ]),
            )
            .order_by(ImportProduct.position)
            .all()
        )
        gids = [r.product_gid for r in rows if r.product_gid]
        names = [r.title or "" for r in rows if r.title]
        # Counted separately so the note can say what actually happened to
        # this range rather than only how big it is. "12 products" reads the
        # same whether all twelve are new or all twelve were already yours.
        already = sum(
            1 for r in rows if r.status == ImportProductStatus.skipped.value
        )
        fresh = len(gids) - already

    body = product_copy.collection_body(title, description, names)

    if dry_run:
        with get_session() as session:
            run = session.get(ImportRun, run_id)
            run.note(f"Dry run: would create the collection “{title}” with {len(gids)} products.")
            _set_stage(session, run, ImportStage.linking, "Cross-linking (dry run).")
        return PassResult(run_id, ImportStage.linking.value, done=False)

    if not gids:
        with get_session() as session:
            run = session.get(ImportRun, run_id)
            run.note("No products were created, so no collection was made.")
            _set_stage(session, run, ImportStage.done, "Finished with nothing to show.")
        return PassResult(run_id, ImportStage.done.value, done=True)

    mode = options.get("collection_mode", "new")
    build_page = options.get("build_page", True)
    tally = f"{fresh} new, {already} already in the store"

    client = _shopify_client()
    existing = client.find_collection(handle)
    collection_gid = None
    if existing:
        # Added to, never rewritten. The page may have been edited by hand
        # since it was made, and a re-import that restored a generated
        # description would silently throw that away.
        collection_gid = existing["id"]
        client.add_products_to_collection(collection_gid, gids)
        message = (
            f"Added {len(gids)} products to the existing “{existing['title']}” "
            f"({tally})."
        )
        if mode == "new":
            message += (
                " Marked as a new range, but a collection with this handle "
                "already existed, so it was added to rather than replaced."
            )
    elif not build_page:
        # An older range whose page the owner maintains themselves. The
        # products are created and tagged either way, so a smart collection
        # built on brand + collection still picks them up.
        message = (
            f"No collection page was built ({tally}). The products carry "
            "their brand and collection tags, so a collection defined on "
            "those will pick them up."
        )
    else:
        created = client.create_collection(
            title=title,
            description_html=body,
            handle=handle,
            seo_title=title[: product_copy.MAX_SEO_TITLE],
            seo_description=(description or body)[: product_copy.MAX_SEO_DESCRIPTION],
            product_gids=gids,
        )
        collection_gid = created["id"]
        message = (
            f"Created the collection “{title}” with {len(gids)} products "
            f"({tally})."
        )

    with get_session() as session:
        run = session.get(ImportRun, run_id)
        run.collection_gid = collection_gid
        # Recorded from the composed title, not the one guessed at discovery
        # time, so the run page links to the collection that exists.
        run.collection_handle = handle
        run.note(message)
        next_stage = (
            ImportStage.linking if options.get("link_products", True) else ImportStage.done
        )
        _set_stage(
            session, run, next_stage,
            "Linking the products to each other."
            if next_stage is ImportStage.linking
            else "Done.",
        )
    return PassResult(run_id, next_stage.value, done=next_stage is ImportStage.done)


# ── Stage 4: link the range together ─────────────────────────────────


def _linking(run_id: int) -> PassResult:
    deadline = time.monotonic() + _budget()
    with get_session() as session:
        run = session.get(ImportRun, run_id)
        dry_run = run.dry_run
        rows = (
            session.query(ImportProduct)
            .filter(
                ImportProduct.run_id == run_id,
                # Products already in the store belong in the grid too. This
                # asked for `created` alone, so the range each product linked
                # to was not the range — it was whichever part of the range
                # that particular run happened to create. Re-importing a
                # collection where eleven of thirteen already existed left
                # every product linking to one other product, and the log
                # said "Cross-linked 2 products" as though that were the
                # whole of it. `_collection` has always used both statuses;
                # this is the same question and deserves the same answer.
                ImportProduct.status.in_([
                    ImportProductStatus.created.value,
                    ImportProductStatus.skipped.value,
                ]),
            )
            .order_by(ImportProduct.position)
            .all()
        )
        siblings = [
            {
                "id": r.id,
                "gid": r.product_gid,
                "title": r.title or "",
                "url": _product_path(r.handle),
                "handle": r.handle,
                "image": _first_image(r.extracted),
                "linked": r.linked,
                "existing": r.status == ImportProductStatus.skipped.value,
            }
            for r in rows
            if r.product_gid
        ]

    todo = [s for s in siblings if not s["linked"]]
    if dry_run or not todo or len(siblings) < 2:
        with get_session() as session:
            run = session.get(ImportRun, run_id)
            if dry_run:
                run.note(
                    f"Dry run finished. {len(siblings)} products prepared — nothing "
                    "was sent to Shopify."
                )
            elif len(siblings) < 2:
                run.note("Only one product, so there was nothing to cross-link.")
            else:
                # Said plainly, because linking a product that was reported
                # "left untouched" a minute earlier does touch it: the
                # description is rewritten to carry the grid. The title,
                # images and documents of an existing product are not.
                existing = sum(1 for s in siblings if s["existing"])
                extra = (
                    f" {existing} of them were already in the store, and had "
                    "their descriptions rewritten to carry the range."
                    if existing else ""
                )
                run.note(f"Cross-linked {len(siblings)} products.{extra}")
            _set_stage(session, run, ImportStage.done, "Done.")
        return PassResult(run_id, ImportStage.done.value, done=True)

    client = _shopify_client()
    handled = 0
    for item in todo:
        try:
            _link_one(client, run_id, item, siblings)
        except Exception as e:  # noqa: BLE001 - a failed link is not a failed product
            log.info("cross-link failed for %s: %s", item["gid"], e)
            with get_session() as session:
                run = session.get(ImportRun, run_id)
                run.note(f"Could not link {item['title']}: {e}"[:300])
                # Marked linked anyway: retrying forever would stall the run
                # on one product whose update Shopify keeps refusing.
                row = session.get(ImportProduct, item["id"])
                if row is not None:
                    row.linked = True
        handled += 1
        if time.monotonic() > deadline:
            break

    return PassResult(
        run_id, ImportStage.linking.value, done=False, handled=handled,
        message=f"{len(todo) - handled} to link",
    )


def _link_one(client, run_id: int, item: dict, siblings: list[dict]) -> None:
    """Rewrite one product's description with its siblings, and record them
    as a metafield the theme can render properly."""
    from blog_pipeline.tools.shopify import ShopifyError

    others = [s for s in siblings if s["gid"] != item["gid"]]
    # A window starting after this product, so each page leads somewhere
    # different rather than every page in the range linking the same eight.
    start = next((i for i, s in enumerate(siblings) if s["gid"] == item["gid"]), 0)
    ordered = (siblings[start + 1:] + siblings[:start])
    limit = _related_limit()
    related = [
        {"url": s["url"], "title": s["title"], "image": s["image"]}
        for s in ordered
        if s["gid"] != item["gid"] and s["url"]
    ][:limit]
    if not related:
        related = [
            {"url": s["url"], "title": s["title"], "image": s["image"]}
            for s in others
            if s["url"]
        ][:limit]

    with get_session() as session:
        row = session.get(ImportProduct, item["id"])
        generated, extracted = row.generated, row.extracted
        source_url = row.source_url
        run = session.get(ImportRun, run_id)
        collection_title = (run.collection_title or "") if run else ""

    copy = _copy_from(generated)
    docs = [
        SourceDoc(url=d.get("url", ""), title=d.get("title"), kind=d.get("kind", "other"),
                  pages=d.get("pages"))
        for d in (extracted.get("docs") or [])
    ]
    doc_urls = _stored_doc_urls(generated)
    body = product_copy.render_description(
        copy,
        docs=docs,
        doc_urls=doc_urls,
        related=related,
        source_url=source_url,
        locale=product_copy.locale_text(),
        brand=product_copy.brand_blurb_text(),
        collection_title=collection_title,
        banner=product_copy.banner_text(),
    )
    client.update_product(item["gid"], description_html=body)

    related_gids = [s["gid"] for s in ordered if s["gid"] != item["gid"]][:limit]
    if related_gids:
        try:
            client.set_metafields([{
                "ownerId": item["gid"],
                "namespace": METAFIELD_NAMESPACE,
                "key": "related_products",
                "type": "list.product_reference",
                "value": json.dumps(related_gids),
            }])
        except ShopifyError as e:
            log.info("related_products metafield failed for %s: %s", item["gid"], e)

    with get_session() as session:
        row = session.get(ImportProduct, item["id"])
        if row is not None:
            row.linked = True


def _copy_from(data: dict) -> product_copy.ProductCopy:
    """The stored copy back as a model, tolerating a row written by an older
    version of the schema."""
    payload = {k: v for k, v in (data or {}).items() if k != "model"}
    payload.setdefault("title", "")
    payload.setdefault("product_type", "")
    payload.setdefault("summary", "")
    payload.setdefault("seo_title", "")
    payload.setdefault("seo_description", "")
    return product_copy.ProductCopy.model_validate(payload)


def _stored_doc_urls(generated: dict) -> dict:
    return dict(generated.get("doc_urls") or {})


def _first_image(extracted: dict) -> str | None:
    images = extracted.get("images") or []
    return images[0].get("url") if images else None


# ── Reading a run ────────────────────────────────────────────────────


def _counts(session, run_id: int) -> dict:
    counts = {s.value: 0 for s in ImportProductStatus}
    rows = (
        session.query(ImportProduct.status)
        .filter(ImportProduct.run_id == run_id)
        .all()
    )
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    counts["total"] = len(rows)
    return counts


def run_status(run_id: int) -> dict:
    with get_session() as session:
        run = session.get(ImportRun, run_id)
        if run is None:
            raise ImportRunError(f"No import run {run_id}.")
        counts = _counts(session, run_id)
        return {
            "id": run.id,
            "stage": run.stage,
            "active": run.is_active,
            "dry_run": run.dry_run,
            "source_url": run.source_url,
            "collection_title": run.collection_title,
            "collection_handle": run.collection_handle,
            "error": run.error,
            "counts": counts,
            "log": run.log[-40:],
            "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        }


def list_runs(limit: int = 25) -> list[dict]:
    with get_session() as session:
        runs = (
            session.query(ImportRun)
            .order_by(ImportRun.started_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": r.id,
                "source_url": r.source_url,
                "collection_title": r.collection_title,
                "stage": r.stage,
                "dry_run": r.dry_run,
                "active": r.is_active,
                "started_at": r.started_at,
                "counts": _counts(session, r.id),
            }
            for r in runs
        ]


def run_detail(run_id: int) -> dict:
    with get_session() as session:
        run = session.get(ImportRun, run_id)
        if run is None:
            raise ImportRunError(f"No import run {run_id}.")
        products = (
            session.query(ImportProduct)
            .filter(ImportProduct.run_id == run_id)
            .order_by(ImportProduct.position)
            .all()
        )
        return {
            "run": run,
            "options": run.options,
            "log": run.log[-60:],
            "counts": _counts(session, run_id),
            "products": [
                {
                    "row": p,
                    "extracted": p.extracted,
                    "generated": p.generated,
                    "image": _first_image(p.extracted),
                    "doc_count": len(p.extracted.get("docs") or []),
                    "spec_count": len(p.extracted.get("specs") or {}),
                }
                for p in products
            ],
        }


def product_preview(run_id: int, product_id: int) -> dict:
    """One product's generated copy, rendered the way Shopify would get it.

    The point of a dry run is to answer "is this good enough to publish",
    and counts of images and specs cannot answer it. This reads back what
    `write_copy` already produced — a dry run does the full scrape, document
    read and model call, and only stops short of the create — and puts it
    through the same `render_description` the real run uses, so what's on
    screen is the body that would be sent, not a summary of it.

    Two things are deliberately honest about being a preview:

    **Downloads are absent.** A dry run never uploads documents, so there
    are no store URLs to link, and `render_downloads` drops a document
    rather than linking the manufacturer's copy — a broken promise of a
    download being worse than no download. The documents that *were* read
    are listed separately, so a thin description that's thin because a PDF
    failed can be told from one that's thin because the source said little.

    **Related products are absent** for the same reason: the cross-link pass
    runs after every product in the collection exists.
    """
    from dashboard.manufacturer import SourceDoc

    with get_session() as session:
        row = session.get(ImportProduct, product_id)
        if row is None or row.run_id != run_id:
            raise ImportRunError(f"No product {product_id} in run {run_id}.")
        run = session.get(ImportRun, run_id)
        if run is None:
            raise ImportRunError(f"No import run {run_id}.")
        session.expunge(row)
        session.expunge(run)

    generated, extracted = row.generated, row.extracted
    docs = [
        SourceDoc(
            url=str(d.get("url") or ""),
            title=d.get("title"),
            kind=str(d.get("kind") or "other"),
            pages=d.get("pages"),
            text=d.get("text"),
            error=d.get("error"),
        )
        for d in (extracted.get("docs") or [])
        if isinstance(d, dict) and d.get("url")
    ]

    body_html = ""
    copy = None
    if generated:
        try:
            copy = product_copy.ProductCopy.model_validate(
                {k: v for k, v in generated.items() if k != "model"}
            )
        except Exception as exc:  # noqa: BLE001 - a preview must never 500
            log.warning("preview: stored copy for %s is unreadable (%s)",
                        product_id, exc)
        else:
            body_html = product_copy.render_description(
                copy,
                docs=docs,
                doc_urls={},
                source_url=row.source_url,
                locale=product_copy.locale_text(),
                brand=product_copy.brand_blurb_text(),
                banner=product_copy.banner_text(),
            )

    tags = list(copy.tags) if copy else list(generated.get("tags") or [])
    source_tag = run.options.get("source_tag")
    if source_tag:
        tags.append(source_tag)

    return {
        "run": run,
        "row": row,
        "copy": copy,
        "generated": generated,
        "extracted": extracted,
        "body_html": body_html,
        "tags": product_copy.clean_tags(tags),
        "docs": docs,
        "images": extracted.get("images") or [],
        "specs": extracted.get("specs") or {},
        "model": generated.get("model"),
    }


def stop_run(run_id: int) -> None:
    """Stop a run where it stands. What it already created stays created.

    Nothing is rolled back, and that is the honest behaviour: the products
    exist in Shopify, drafts or not, and quietly deleting them because
    someone clicked Stop would be a bigger surprise than leaving them.
    """
    with get_session() as session:
        run = session.get(ImportRun, run_id)
        if run is None or not run.is_active:
            return
        run.stage = ImportStage.stopped.value
        run.finished_at = _now()
        run.note("Stopped. Anything already created is still in the store.")


def active_run_ids(limit: int = 5) -> list[int]:
    """Runs that still have work to do — what the cron job advances."""
    with get_session() as session:
        rows = (
            session.query(ImportRun.id)
            .filter(
                ImportRun.stage.notin_([
                    ImportStage.done.value,
                    ImportStage.failed.value,
                    ImportStage.stopped.value,
                ])
            )
            .order_by(ImportRun.started_at)
            .limit(limit)
            .all()
        )
        return [row.id for row in rows]
