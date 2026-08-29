"""Turning a scraped product into something worth publishing.

The scraper hands over facts: a title, whatever the manufacturer wrote, a
spec table, and the text of the PDFs. This module turns that into the product
page — description, SEO fields, tags, FAQ — and it is deliberately split in
two halves:

**The model writes the words. The code builds the page.** `write_copy` asks
for structured fields (paragraphs, bullets, spec pairs, questions and
answers) and nothing else; `render_description` assembles the HTML from them.
So a hallucinated `<script>`, a broken table, or a model that decides today's
description should be a poem cannot reach the storefront — the worst case is
weak prose in a correct page.

**Nothing is invented from nothing.** Every specification the model is
allowed to state comes from the source page or the PDFs, both of which are
quoted into the brief, and the prompt says so. This matters more for products
than for blog posts: "20-year residential warranty" on a product page is a
claim the business has to honour, and a model that rounds 15 up to 20 because
it reads better has created a liability, not a typo.

The SEO and GEO work is the same shape as the blog pipeline's (see
`blog_pipeline.agents.seo` and `.geo`):

  * **SEO** — a meta title under 60 characters and a description under 155
    that read as a search result rather than a summary, both keyed on how
    someone shops for this product rather than the manufacturer's name for it.
  * **GEO** (generative engine optimization — being citable by AI answer
    engines) — self-contained, extractable blocks: a specifications table an
    answer engine can read as data, a FAQ section whose answers stand alone,
    and `FAQPage` JSON-LD. Product JSON-LD is deliberately *not* emitted here:
    Shopify themes already emit `schema.org/Product` for the product, and a
    second, subtly different copy inside the description body competes with
    the theme's rather than adding to it.
  * **Local** — a short availability line naming the service area, because
    "in stock in Langley" is the difference between a national product page
    and a local one for both Google and a customer.
"""

from __future__ import annotations

import html
import json
import logging
import re

from pydantic import BaseModel, Field

from dashboard import store
from dashboard.manufacturer import SourceDoc, SourceProduct
from dashboard.product_docs import doc_digest, filename_for

log = logging.getLogger(__name__)

MAX_SEO_TITLE = 60
MAX_SEO_DESCRIPTION = 155
MAX_TAGS = 20


# ── What the model is asked for ──────────────────────────────────────


class SpecPair(BaseModel):
    name: str = Field(description="Specification name, e.g. 'Wear layer'")
    value: str = Field(description="Its value exactly as the source states it")


class FaqItem(BaseModel):
    question: str = Field(description="A question a buyer actually asks")
    answer: str = Field(
        description=(
            "A complete answer in 2-4 sentences that makes sense on its own, "
            "without the rest of the page"
        )
    )


class ProductCopy(BaseModel):
    """The product page as fields. Rendered to HTML by this module, never by
    the model."""

    title: str = Field(description="Product title for our store, under 70 characters")
    size: str = Field(
        default="",
        description=(
            "The nominal size exactly as the source writes it, units and "
            "punctuation included — '5\"x10\"', '7mm', '12x24'. Empty if "
            "the source does not state one. Never invent or convert it."
        ),
    )
    color: str = Field(
        default="",
        description=(
            "The colour, decor or finish name that distinguishes THIS item "
            "from the rest of its range, and nothing else — 'Bassano', "
            "'Emerald Bevel Gloss', 'Natural Oak'. Not the range name, not "
            "the size, not the material. Empty if the range has only one."
        ),
    )
    product_type: str = Field(description="Shopify product type, e.g. 'Wall Tile'")
    summary: str = Field(description="One sentence: what this is and who it suits")
    paragraphs: list[str] = Field(
        default_factory=list,
        description="2-3 paragraphs of description. No headings, no HTML.",
    )
    features: list[str] = Field(
        default_factory=list,
        description="4-6 short factual bullet points drawn from the source",
    )
    specs: list[SpecPair] = Field(
        default_factory=list,
        description="Specifications from the source page or documents only",
    )
    applications: list[str] = Field(
        default_factory=list,
        description="Where this product is suitable to use, 3-6 short items",
    )
    faqs: list[FaqItem] = Field(
        default_factory=list, description="3-5 buyer questions with self-contained answers"
    )
    tags: list[str] = Field(
        default_factory=list,
        description=(
            "8-15 Shopify tags: material, colour, finish, size, room, style, "
            "application. Short noun phrases, no hashes."
        ),
    )
    seo_title: str = Field(description=f"Under {MAX_SEO_TITLE} characters")
    seo_description: str = Field(description=f"Under {MAX_SEO_DESCRIPTION} characters")
    image_alts: list[str] = Field(
        default_factory=list,
        description="Alt text for each product image, in order, describing what is shown",
    )


_SYSTEM = """\
You write product pages for a flooring and tile retailer's online store.

The ONLY facts available to you are in the SOURCE block. It is the
manufacturer's own page and their PDF documentation. Rules, in order of
importance:

1. Never state a specification, certification, warranty term, rating or
   measurement that is not in the SOURCE. If the source doesn't give it,
   leave it out. A shorter page is always better than an invented number.
2. Copy specification values exactly as the source states them, units and
   all. Do not convert, round or tidy them.
3. Write for a buyer choosing between products, not for the manufacturer's
   marketing department. Plain, concrete, specific.
4. No hype ("revolutionary", "unparalleled"), no second-person sales patter,
   no claims about price, stock or delivery.
5. FAQ answers must stand alone: someone reading only that answer, with no
   other context, should get a complete and correct one.
6. The SEO title and description are what appears in Google's results. Lead
   with what the product is, not the brand's internal range name.
7. `product_type` is how a SHOPPER would describe the thing — "Porcelain
   Wall Tile", "Ceramic Wall Tile", "Waterproof Luxury Vinyl Plank". It
   must end in the format being sold (Tile, Plank, Mosaic, Moulding), and
   must never be a specification value copied off the page: "Glazed
   Ceramic/Non-porcelain" is a body classification, not a product type. Say
   "Glazed Ceramic Wall Tile" instead. The store's product titles are built
   from this field, so it appears on the shelf.
8. `color` is the one field that separates this item from its siblings. A
   manufacturer usually puts it in the product title after the range name
   and the size — "3D Bars | 5\"x10\" Emerald Bevel Gloss" is the range,
   the size, then the colour. Give the colour alone. The store's own title
   is assembled from it, so a size or a range name here ends up duplicated
   on the shelf.
"""


def build_brief(
    source: SourceProduct,
    *,
    collection_title: str | None = None,
    collection_description: str | None = None,
    vendor: str | None = None,
    locale: str = "",
) -> str:
    """Everything the model gets. Facts only, labelled by where they came from."""
    lines: list[str] = ["SOURCE", f"Product URL: {source.source_url}"]
    if source.title:
        lines.append(f"Manufacturer title: {source.title}")
    if vendor or source.vendor:
        lines.append(f"Brand: {vendor or source.vendor}")
    if source.sku:
        lines.append(f"SKU: {source.sku}")
    if collection_title:
        lines.append(f"Part of the manufacturer's collection: {collection_title}")
    if collection_description:
        lines.append(f"Collection description: {collection_description[:1000]}")
    if source.product_type:
        lines.append(f"Manufacturer product type: {source.product_type}")
    if source.tags:
        lines.append(f"Manufacturer tags: {', '.join(source.tags[:30])}")
    if source.options:
        rendered = "; ".join(
            f"{name}: {', '.join(values[:20])}" for name, values in source.options.items()
        )
        lines.append(f"Variants offered by the manufacturer: {rendered}")
    if source.specs:
        lines.append("Specifications published on the page:")
        for name, value in list(source.specs.items())[:40]:
            lines.append(f"  - {name}: {value}")
    if source.description_text:
        lines.append(f"\nManufacturer description:\n{source.description_text[:6000]}")

    digest = doc_digest(source.docs)
    if digest:
        lines.append(
            "\nTEXT EXTRACTED FROM THE MANUFACTURER'S DOCUMENTS "
            "(the most reliable specifications are here):\n" + digest
        )
    unread = [d for d in source.docs if not d.text and d.error]
    if unread:
        lines.append(
            "\nDocuments that could not be read (do not guess at their "
            "contents): "
            + "; ".join(f"{d.title or d.url} ({d.error})" for d in unread[:6])
        )
    if locale:
        lines.append(
            f"\nOUR STORE: sells and installs in {locale}. You may mention the "
            "service area once, factually. Never claim stock levels, lead "
            "times or prices."
        )
    lines.append(
        f"\nThe product has {len(source.images)} images, so write exactly "
        f"{len(source.images)} alt texts."
    )
    return "\n".join(lines)


def locale_text() -> str:
    """Where the store sells, for the local-availability line.

    Built from the pipeline's `business_location` plus the cities the local
    rank tracker already watches, so the product page's service area and the
    one the dashboard measures are the same list rather than two guesses.
    """
    from dashboard.config import pipeline

    settings = pipeline()
    primary = (settings.business_location or "").strip()
    cities: list[str] = []
    for entry in store.get(store.LOCAL_CITIES) or []:
        name = str(entry).split(":", 1)[0].strip()
        if name and name.lower() not in {c.lower() for c in cities}:
            cities.append(name)
    if primary and cities:
        others = [c for c in cities if c.lower() != primary.lower()][:4]
        return f"{primary}" + (f" and the surrounding area ({', '.join(others)})" if others else "")
    return primary or ", ".join(cities[:5])


def write_copy(
    source: SourceProduct,
    *,
    collection_title: str | None = None,
    collection_description: str | None = None,
    vendor: str | None = None,
    model: str | None = None,
) -> tuple[ProductCopy, str]:
    """Write the page for one product. Returns the copy and the model used.

    Falls back to `fallback_copy` rather than raising: an import that gets 40
    products in with plain descriptions is recoverable — the owner can
    regenerate one — and an import that dies on product 12 because a free-tier
    model was rate-limited is not.
    """
    from blog_pipeline.llm import has_access_for, is_openrouter_model, structured_invoke

    chosen = model or store.get(store.IMPORT_MODEL)
    brief = build_brief(
        source,
        collection_title=collection_title,
        collection_description=collection_description,
        vendor=vendor,
        locale=locale_text(),
    )
    if not has_access_for(chosen):
        log.info("no API key configured for %s; using the deterministic description", chosen)
        # Tidied like any other copy. Skipping it here was a real bug: the
        # fallback derives seo_title from the product title, which is the one
        # value Shopify silently stores as null — so every product imported
        # without a key would have had no meta title at all.
        return tidy(fallback_copy(source, vendor=vendor), source), "none"

    try:
        copy = structured_invoke(
            model=chosen,
            schema=ProductCopy,
            messages=[("system", _SYSTEM), ("human", brief)],
            temperature=0.3,
            stage="product_copy",
            max_tokens=4000,
            # A deliberately chosen OpenRouter model falling back to the
            # pipeline's Gemini fallback chain would swap providers (and
            # need a Google key that may not exist) with no sign why the
            # product page suddenly reads differently.
            fallbacks=[] if is_openrouter_model(chosen) else None,
        )
    except Exception as e:  # noqa: BLE001 - one product's copy is not the run
        log.warning("copy generation failed for %s: %s", source.source_url, e)
        return (
            tidy(fallback_copy(source, vendor=vendor), source),
            f"failed: {type(e).__name__}",
        )

    return tidy(copy, source), chosen


def fallback_copy(source: SourceProduct, *, vendor: str | None = None) -> ProductCopy:
    """A correct, plain product page built from the source with no model.

    Used when there is no API key and when generation fails. Everything here
    is copied, never written, which is why it's safe to publish unattended:
    the worst case is a thin page, not a wrong one.
    """
    title = source.title or source.handle.replace("-", " ").title()
    sentences = re.split(r"(?<=[.!?])\s+", source.description_text or "")
    paragraphs = [s.strip() for s in sentences if s.strip()][:6]
    specs = [SpecPair(name=k, value=v) for k, v in list(source.specs.items())[:20]]
    return ProductCopy(
        title=title[:70],
        product_type=source.product_type or "",
        summary=(paragraphs[0] if paragraphs else title)[:200],
        paragraphs=[" ".join(paragraphs[:3])] if paragraphs else [],
        features=[f"{s.name}: {s.value}" for s in specs[:6]],
        specs=specs,
        applications=[],
        faqs=[],
        tags=[t for t in source.tags[:MAX_TAGS]],
        seo_title=title[:MAX_SEO_TITLE],
        seo_description=(
            (paragraphs[0] if paragraphs else f"{title} from {vendor or 'our range'}.")
        )[:MAX_SEO_DESCRIPTION],
        image_alts=[i.alt or title for i in source.images],
    )


# ── Tidying what came back ───────────────────────────────────────────


def tidy(copy: ProductCopy, source: SourceProduct) -> ProductCopy:
    """Enforce the limits Shopify and Google enforce, before they do.

    Shopify silently truncates nothing here, but Google truncates a long meta
    title in the results page and a `seo_title` identical to the product
    title is discarded outright (see `product_seo.py`), so both are handled
    here rather than discovered later.
    """
    copy.title = _one_line(copy.title)[:70] or source.title[:70]
    copy.seo_title = _one_line(copy.seo_title)[:MAX_SEO_TITLE]
    copy.seo_description = _one_line(copy.seo_description)[:MAX_SEO_DESCRIPTION]
    if not copy.seo_title:
        copy.seo_title = copy.title[:MAX_SEO_TITLE]
    if copy.seo_title.strip().lower() == copy.title.strip().lower():
        # Shopify stores null for an SEO title equal to the product title, so
        # a page that wanted a custom one would silently get none.
        suffix = source.vendor or "Specs & Pricing"
        room = MAX_SEO_TITLE - len(suffix) - 3
        copy.seo_title = f"{copy.title[:max(room, 10)]} | {suffix}"[:MAX_SEO_TITLE]
    copy.tags = clean_tags(copy.tags + source.tags)
    copy.paragraphs = [_one_line(p) for p in copy.paragraphs if _one_line(p)][:4]
    copy.features = [_one_line(f) for f in copy.features if _one_line(f)][:8]
    copy.applications = [_one_line(a) for a in copy.applications if _one_line(a)][:8]
    copy.faqs = [f for f in copy.faqs if f.question.strip() and f.answer.strip()][:6]
    copy.specs = [
        SpecPair(name=_one_line(s.name)[:80], value=_one_line(s.value)[:300])
        for s in copy.specs
        if _one_line(s.name) and _one_line(s.value)
    ][:30]
    # One alt per image, in order: the renderer pairs them positionally, and a
    # short list would leave later images unlabelled.
    alts = [_one_line(a)[:250] for a in copy.image_alts if _one_line(a)]
    while len(alts) < len(source.images):
        alts.append(copy.title)
    copy.image_alts = alts[: len(source.images)]
    return copy


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


#: Longest a composed title may get. Shopify accepts more, but a title that
#: wraps to three lines in search results and in the admin list is one
#: nobody can scan.
MAX_TITLE = 160


def compose_title(
    *, brand: str | None, collection: str | None,
    product_type: str | None, color: str | None, size: str | None = None,
    fallback: str = "",
) -> str:
    """The store's naming standard: brand, range, type, size, then colour.

    "EUROSTYLE Venice Grand PRO Waterproof Luxury Vinyl Plank - Bassano"
    "Ames Tile 3D Bars Porcelain Wall Tile 5\"x10\" - Emerald Bevel Gloss"

    The size sits with the type because that is what it qualifies, and it is
    omitted when the source doesn't state one rather than guessed at — a
    range sold in one size shouldn't grow a size in its name.

    Assembled here rather than asked of the model, because a standard a
    model is *asked* to follow is a standard that holds for most of a
    catalogue. The parts are things the app already knows — the vendor, the
    collection being imported, the product type — and only the colour needs
    reading off the source, which is why that is the single field the model
    contributes.

    Each part is dropped when it's missing rather than leaving a gap or a
    stray dash, and a repeated part is dropped too: a manufacturer whose
    range name already contains the brand would otherwise produce
    "EUROSTYLE EUROSTYLE Venice".
    """
    # Without a colour there is nothing in the standard that separates one
    # item in a range from the next, so every product would compose to the
    # same name — and the handle is derived from the name, so the second
    # onwards would be skipped as "already in the store". A range silently
    # importing as one product is far worse than a name off-standard, so the
    # source's own title wins whenever the discriminator is missing.
    if not _one_line(color or "").strip(" -|,"):
        return _one_line(fallback)[:MAX_TITLE].strip()

    parts: list[str] = []
    for raw in (brand, collection, product_type, size):
        piece = _one_line(raw or "").strip(" -|,")
        if not piece:
            continue
        # Overlap runs both ways. A range named "Venice Grand PRO" under the
        # brand "EUROSTYLE" is fine, but manufacturers also publish the range
        # as "EUROSTYLE Venice Grand PRO" — so the part already said is
        # trimmed off the front of the next one rather than only skipping an
        # exact repeat.
        for said in parts:
            if piece.lower().startswith(said.lower()):
                piece = piece[len(said):].strip(" -|,")
        if not piece or piece.lower() in " ".join(parts).lower():
            continue
        parts.append(piece)

    stem = " ".join(parts).strip()
    shade = _one_line(color).strip(" -|,")
    if shade.lower() not in stem.lower():
        stem = f"{stem} - {shade}" if stem else shade
    return (stem or _one_line(fallback))[:MAX_TITLE].strip()


def clean_tags(tags: list[str]) -> list[str]:
    """De-duplicate case-insensitively, drop junk, cap the count.

    Tags are how the storefront filters and how the show-price mechanism
    works on this store, so they're data, not decoration — a tag list with
    `Tile`, `tile` and `TILE` in it is three filters where there should be
    one.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in tags:
        tag = _one_line(raw).strip("#,;").strip()
        if not tag or len(tag) > 60:
            continue
        key = tag.lower()
        if key in seen or key in {"n/a", "none", "product"}:
            continue
        seen.add(key)
        out.append(tag)
        if len(out) >= MAX_TAGS:
            break
    return out


def slugify(text: str, *, fallback: str = "product") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (slug or fallback)[:100].strip("-")


# ── Rendering ────────────────────────────────────────────────────────


def _esc(text: str) -> str:
    return html.escape(str(text or "").strip())


def render_description(
    copy: ProductCopy,
    *,
    docs: list[SourceDoc] | None = None,
    doc_urls: dict[str, str] | None = None,
    related: list[dict] | None = None,
    source_url: str | None = None,
    locale: str = "",
    brand: str = "",
    collection_title: str = "",
    banner: str = "",
) -> str:
    """The product description body, assembled from the copy's fields.

    Sections are ordered the way someone reads a product page — what it is,
    why it suits them, what it's made of, where it goes, what to download,
    what else is in the range — and each one is omitted entirely when it has
    no content, so a thin source produces a short page rather than a page of
    empty headings.
    """
    parts: list[str] = []

    banner_html = render_banner(banner)
    if banner_html:
        parts.append(banner_html)

    if copy.summary:
        parts.append(f'<p class="product-summary"><strong>{_esc(copy.summary)}</strong></p>')
    for paragraph in copy.paragraphs:
        parts.append(f"<p>{_esc(paragraph)}</p>")

    if copy.features:
        items = "".join(f"<li>{_esc(f)}</li>" for f in copy.features)
        parts.append(
            f'<div class="product-highlights"><h2>Key features</h2><ul>{items}</ul></div>'
        )

    if copy.specs:
        rows = "".join(
            f"<tr><th scope=\"row\">{_esc(s.name)}</th><td>{_esc(s.value)}</td></tr>"
            for s in copy.specs
        )
        parts.append(
            '<div class="product-specs"><h2>Specifications</h2>'
            f"<table><tbody>{rows}</tbody></table></div>"
        )

    if copy.applications:
        items = "".join(f"<li>{_esc(a)}</li>" for a in copy.applications)
        parts.append(f"<div class=\"product-uses\"><h2>Where to use it</h2><ul>{items}</ul></div>")

    if locale:
        parts.append(
            '<p class="product-local">Available to order through our '
            f"showroom, serving {_esc(locale)}. Ask us for a sample or a "
            "measured quote.</p>"
        )

    blurb = render_brand_blurb(brand)
    if blurb:
        parts.append(blurb)

    downloads = render_downloads(docs or [], doc_urls or {})
    if downloads:
        parts.append(downloads)

    if copy.faqs:
        blocks = "".join(
            f"<div class=\"faq-item\"><h3>{_esc(f.question)}</h3>"
            f"<p>{_esc(f.answer)}</p></div>"
            for f in copy.faqs
        )
        parts.append(
            '<section class="product-faq"><h2>Frequently asked questions</h2>'
            f"{blocks}</section>"
        )

    if related:
        parts.append(render_related(related, collection_title))

    faq_ld = faq_jsonld(copy)
    if faq_ld:
        parts.append(faq_ld)

    return "\n".join(parts)


#: A bare URL in the blurb, so the store's own address can be a link without
#: the setting having to accept HTML.
_BARE_URL = re.compile(r"(https?://[^\s<>\"']+|www\.[^\s<>\"']+)", re.I)

#: Guard against a paste that runs away. The description is a product page,
#: not a newsletter.
MAX_BLURB = 4000


# Bold and a touch larger than body copy, but no colour: the storefront's
# own palette should decide that, and a hardcoded brand colour here would be
# the one thing on the page that clashes after a theme change.
_BANNER_STYLE = "font-weight:600;font-size:1.05em;margin:0 0 12px"


def banner_text() -> str:
    """The line that opens every product page, read from settings."""
    from dashboard import store

    return str(store.get(store.IMPORT_TOP_BANNER) or "")


def render_banner(text: str) -> str:
    """The call-to-action at the top of the description.

    First thing on the page because it is the only thing on the page asking
    for the conversion this store actually has. Almost nothing here is bought
    online — 94% of the catalogue hides its price behind "Call for price" —
    so a product page whose first line is a specification is a page that
    buries its own purpose.

    Escaped and linkified exactly like the brand block, and for the same
    reason: a settings field that can put arbitrary markup at the top of
    every product in the store is not a trade worth making for one link.
    """
    raw = str(text or "").strip()[:400]
    if not raw:
        return ""
    return (
        f'<p class="product-banner" style="{_BANNER_STYLE}">'
        f"{_linkify(raw)}</p>"
    )


def brand_blurb_text() -> str:
    """The configured brand block, read from settings.

    Separate from the renderer for the same reason `locale_text` is separate
    from the paragraph it feeds: rendering is pure and testable without a
    database, and every caller that builds a description already looks up
    the store's own facts before calling it.
    """
    from dashboard import store

    return str(store.get(store.IMPORT_BRAND_BLURB) or "")


def render_brand_blurb(text: str) -> str:
    """The store's own block: what we stock, what's on offer, and to call.

    Kept in settings rather than written here because it is the part of a
    product page most likely to be wrong tomorrow — a delivery threshold, a
    list of cities, a seasonal offer. Code would need a deploy to correct
    across a catalogue that is already published.

    Escaped, then bare URLs are turned into links. That combination is
    deliberate: the owner wants their own address to be clickable, and
    accepting HTML to get one link would mean a settings field that can put
    arbitrary markup on every product in the store.
    """
    body = _linkify_paragraphs(text, limit=MAX_BLURB)
    return f'<div class="product-brand">{body}</div>' if body else ""


def _linkify(chunk: str) -> str:
    """One line of owner-written text, safe to put in a description.

    Unescaped first, then escaped. That looks like a no-op and isn't: the
    owner pastes text out of a browser or an old product page, so it arrives
    carrying entities — a leading `&nbsp;` for indentation is the common one
    — and escaping those directly publishes the literal characters
    "&nbsp;" to the customer. Decoding first turns it into the space it was
    meant to be, and escaping after means the round trip still cannot emit
    markup: `&lt;script&gt;` decodes to `<script>` and re-encodes right back.
    """
    text = html.unescape(str(chunk))
    pieces: list[str] = []
    last = 0
    for match in _BARE_URL.finditer(text):
        pieces.append(_esc(text[last:match.start()]))
        url = match.group(0).rstrip(".,;:)")
        trailing = match.group(0)[len(url):]
        href = url if url.lower().startswith("http") else f"https://{url}"
        pieces.append(
            f'<a href="{_esc(href)}" rel="noopener" target="_blank">'
            f"{_esc(url)}</a>{_esc(trailing)}"
        )
        last = match.end()
    pieces.append(_esc(text[last:]))
    return "".join(pieces)


def _linkify_paragraphs(text: str, *, limit: int) -> str:
    """Owner-written text as paragraphs, blank lines splitting them."""
    raw = str(text or "").strip()[:limit]
    if not raw:
        return ""
    paragraphs = [
        "<p>" + _linkify(chunk.strip()).replace("\n", "<br>") + "</p>"
        for chunk in re.split(r"\n\s*\n", raw)
        if chunk.strip()
    ]
    return "".join(paragraphs)


def render_downloads(docs: list[SourceDoc], doc_urls: dict[str, str]) -> str:
    """Links to the documents, pointing at our copies on Shopify's CDN.

    Our copies, not the manufacturer's URLs: a supplier reorganising their
    site shouldn't turn every spec-sheet link in the store into a 404, and a
    customer downloading a datasheet shouldn't leave the site to do it. A doc
    that failed to upload falls back to nothing rather than to the source
    link — a broken promise of a download is worse than no download.
    """
    items: list[str] = []
    for doc in docs:
        url = doc_urls.get(doc.url)
        if not url:
            continue
        label = doc.title or filename_for(doc)
        kind = doc.kind.replace("_", " ").title()
        pages = f" · {doc.pages} pages" if doc.pages else ""
        items.append(
            f'<li><a href="{_esc(url)}" target="_blank" rel="noopener">'
            f"{_esc(label)}</a> <small>({_esc(kind)}{pages})</small></li>"
        )
    if not items:
        return ""
    return (
        '<div class="product-downloads"><h2>Downloads</h2><ul>'
        + "".join(items)
        + "</ul></div>"
    )


# Inline styles, not classes. This markup is written into a Shopify product
# description, and the theme rendering it has never heard of
# `.related-grid` — so a class-only version fell back to browser defaults:
# a bulleted list, one full-width image per row, the title floating beside
# it. Whatever the theme's own CSS is, these rules ship with the content.
#
# Kept to layout only. Nothing here sets a colour or a font, so the block
# still inherits the storefront's typography instead of fighting it.
_GRID_STYLE = (
    "list-style:none;padding:0;margin:16px 0 0;display:grid;"
    "grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:18px"
)
_CARD_STYLE = "margin:0;padding:0;list-style:none"
_LINK_STYLE = "display:block;text-decoration:none"
_IMG_STYLE = (
    "display:block;width:100%;height:auto;aspect-ratio:1/1;"
    "object-fit:cover;border-radius:6px;margin:0"
)
_CAPTION_STYLE = "display:block;margin-top:8px;line-height:1.35"


def render_related(related: list[dict], collection_title: str = "") -> str:
    """The rest of the collection, with a picture each.

    Named rather than generic: "More from this collection" tells a reader
    nothing they can search for, while the range's own name is the phrase
    they would type. It also gives the internal links an anchor context that
    says what the range is.

    Rendered into the body rather than left to a theme section so it works on
    any theme, and so the links exist as HTML for a crawler — internal links
    between the products in a range are how a collection gets found as a
    range rather than as unrelated pages.
    """
    cards: list[str] = []
    for item in related:
        url = item.get("url")
        title = item.get("title")
        if not url or not title:
            continue
        image = item.get("image")
        picture = (
            f'<img src="{_esc(image)}" alt="{_esc(title)}" loading="lazy" '
            f'width="300" height="300" style="{_IMG_STYLE}">'
            if image
            else ""
        )
        cards.append(
            f'<li class="related-product" style="{_CARD_STYLE}">'
            f'<a href="{_esc(url)}" style="{_LINK_STYLE}">{picture}'
            f'<span style="{_CAPTION_STYLE}">{_esc(title)}</span></a></li>'
        )
    if not cards:
        return ""
    name = _one_line(collection_title or "").strip()
    heading = (
        f"If you want to see more products from {_esc(name)}, you can find "
        "them in the following list:"
        if name
        else "More from this collection"
    )
    return (
        f'<div class="product-related"><h2>{heading}</h2>'
        f'<ul class="related-grid" style="{_GRID_STYLE}">'
        + "".join(cards)
        + "</ul></div>"
    )


def faq_jsonld(copy: ProductCopy) -> str:
    """`FAQPage` structured data for the questions on the page.

    The single highest-leverage thing on a product page for AI answer engines
    and rich results, and the one piece of schema a Shopify theme doesn't
    already emit for a product — so unlike `Product` schema, adding it here
    competes with nothing.

    Whether a `<script>` survives in a product description depends on the
    store's theme and on Shopify's own sanitising of the field, which is why
    the importer also writes the same questions to a `custom.faq` metafield.
    If the tag is stripped, the visible FAQ section still stands and only the
    structured data is lost.
    """
    if not copy.faqs:
        return ""
    payload = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": faq.question.strip(),
                "acceptedAnswer": {"@type": "Answer", "text": faq.answer.strip()},
            }
            for faq in copy.faqs
        ],
    }
    # `</script>` inside a JSON string would close the tag early; escaping the
    # slash is the conventional fix and stays valid JSON.
    encoded = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return f'<script type="application/ld+json">{encoded}</script>'


def collection_body(title: str, description: str | None, products: list[str]) -> str:
    """A short body for the collection page itself.

    A collection with an empty description is a page with a heading and a
    grid, which ranks for nothing. This gives it at least a sentence and the
    names of what's in it.
    """
    parts: list[str] = []
    if description:
        parts.append(f"<p>{_esc(description)}</p>")
    else:
        parts.append(
            f"<p>Browse the {_esc(title)} range. Full specifications, "
            "manufacturer documentation and samples available on each product "
            "page.</p>"
        )
    if products:
        items = "".join(f"<li>{_esc(name)}</li>" for name in products[:40])
        parts.append(f"<div class=\"collection-contents\"><h2>In this range</h2><ul>{items}</ul></div>")
    return "\n".join(parts)
