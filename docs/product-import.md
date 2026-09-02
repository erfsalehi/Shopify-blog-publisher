# Product import — a manufacturer's collection into the store

Paste the URL of a collection on a manufacturer's own site. Everything in it
is read, its documents are downloaded and used, and each product is created
here as a draft with its own copy, SEO fields, tags, photographs, downloads
and links to the rest of the range. The collection is created too.

The page is **Import** in the top nav (`/import`).

## Before the first run

The Shopify token needs two scopes the blog pipeline never asked for:
**`write_products`** (products, media, collections, metafields) and
**`write_files`** (re-hosting the manufacturer's PDFs). Publishing to sales
channels additionally needs **`write_publications`**; without it the products
are still created and the run still finishes, with a warning in the log and
whichever channels their own auto-publish setting happened to cover. Shopify admin →
Settings → Apps and sales channels → Develop apps → your app → Configure
Admin API scopes → tick both → **Save**, then **Install app** again. The
token string doesn't change.

Without them the first product fails with a Shopify `userErrors` message
naming the missing scope, and the run stops there having created nothing —
which is why a dry run costs nothing to try first.

```
https://www.example.com/collections/some-range
        │
        ├─ discover ── which products the collection lists
        ├─ products ── per product: scrape → read PDFs → write copy → create
        ├─ collection ─ create it, put the products in it
        └─ linking ─── rewrite each description with its siblings
```

## What it does with a product

| Source | Becomes |
|---|---|
| Manufacturer title, description | Product title and description body |
| Product photography | Images on the product, alt text written for each |
| Spec table / JSON-LD `additionalProperty` | A specifications table, and a `custom.specifications` metafield |
| Linked PDFs | Downloaded, read for their specs, re-hosted on your store, linked in a Downloads section and a `custom.documents` metafield |
| Everything above | SEO title and description, tags, an FAQ with `FAQPage` JSON-LD, a local availability line |
| The other products in the collection | A "More from this collection" block with thumbnails, and a `custom.related_products` metafield |

The manufacturer's page URL is kept on `custom.source_url` and linked at the
foot of the description, so six months from now "where did this spec come
from" has an answer.

## What it deliberately doesn't do

**Prices.** Manufacturer pages publish trade or no prices at all, and a price
on your storefront is a promise. Products are created with Shopify's default
variant and no price set — the same "call for price" state most of this
catalogue is already in.

**Variants.** Sizes and finishes offered by the manufacturer are recorded (in
the specs, the tags and the description) but not turned into Shopify variants.
Without prices a variant carries almost nothing, and getting variant structure
wrong is expensive to undo. If you want them, it's the obvious next feature.

**Overwrite anything, unless you tell it to.** Before creating a product, the
importer asks Shopify whether that handle already exists. If it does, the
product is left completely alone and the run records it as `skipped`.
Re-running the same import is therefore safe and is the normal way to pick up
products a supplier added. When that call is wrong — see [Importing a skipped
product anyway](#importing-a-skipped-product-anyway) — you can overrule it
per product or for the whole run, and it is only ever you who does.

**Publish.** Products are created as **drafts** unless you pick Active on the
form. Nothing reaches the storefront until you say so in Shopify admin.

## Dry run

Tick **Dry run** and the whole pipeline runs — the collection is read, every
product page fetched, every PDF downloaded and parsed, all the copy written —
and nothing at all is sent to Shopify. The run page then shows exactly what
was extracted and what would have been written per product.

This is the right way to try a supplier's site for the first time. Sites
differ wildly in what they publish, and a dry run tells you in a minute
whether this one yields real specifications or three sentences of marketing.

## How a run makes progress

A run is stages, not one long function, because a Vercel function is killed at
60 seconds and a forty-product collection is minutes of work. Each pass does a
bounded slice and writes down where it got to. Three things advance a run:

1. **The run page**, while it's open. It asks for one pass, shows the result,
   asks for the next. This is the fastest path and the one you watch.
2. **The `product_import` job**, hourly on the deployment (`vercel.json`) and
   nightly on the local scheduler. This is what finishes a run you walked away
   from. Each tick keeps advancing until the import is done or the tick runs
   out of wall clock.
3. **Continue now** on the run page, for one pass on demand.

Closing the tab pauses a run; it never loses one. **Stop** ends it where it
stands — whatever was already created stays created, because it exists in
Shopify and quietly deleting it would be the bigger surprise.

## Where the information comes from

In order of preference, per `dashboard/manufacturer.py`:

1. **Shopify's collection JSON** (`/collections/<handle>/products.json`) when
   the manufacturer runs Shopify — the whole product, images and all, in one
   request per 250 products. This is by far the best case.
2. **JSON-LD** (`schema.org/Product`, `ItemList`), which sites emit for Google.
3. **OpenGraph** tags.
4. **The page markup** — the `<h1>`, the spec `<table>`, the `<img>` tags, and
   the `<a href="…pdf">` links.
5. **Firecrawl**, only when a collection page comes back with nothing to
   parse and `FIRECRAWL_API_KEY` is set — a real browser renders the page's
   JavaScript, and steps 2-4 run again against what that produces.

Each step fills what the ones before it didn't, except documents and specs,
which are always additive — a warranty PDF found in the markup joins the spec
sheet the feed already knew about. What produced each field is recorded in
`sources` on the extraction, so a thin result is diagnosable without re-running
it.

`robots.txt` is honoured, requests are paced a second apart, and the
User-Agent says who this is and why. If a site disallows the path, the run
fails with that as the reason rather than fetching it anyway.

## Reading the PDFs

Spec sheets are where the real product information lives, so they're
downloaded and parsed (`dashboard/product_docs.py`), and their text goes into
the brief the copy is written from. Limits: 25 MB per document, the first 12
pages, 20,000 characters, and by default four documents per product — ordered
spec sheet, installation guide, warranty, care guide, since that's the order
they matter in.

A scan with no text layer, a password-protected file, a 404 — each records why
on the document and the import carries on. The file is still uploaded and
still linked; it just didn't contribute any words.

## The copy

One model call per product (`dashboard/product_copy.py`). The model is asked
for **fields** — paragraphs, bullets, spec pairs, questions and answers — and
this code assembles the HTML from them, so a malformed response can't produce
a broken product page.

The prompt's first rule is that the only facts available are the ones in the
brief, which is built from the source page and the PDFs. Specifications are to
be copied exactly, units and all. On a product page a rounded warranty term is
not a typo, it's a claim the business has to honour.

If there's no `GOOGLE_API_KEY`, or the call fails, the import falls back to a
plain description assembled from the source with no model involved. Thin, but
never wrong — and the product still gets created rather than the run stalling.

### SEO and GEO

* **SEO** — meta title under 60 characters, description under 155, written as
  a search result. A meta title identical to the product title is rewritten,
  because Shopify silently discards that case and stores null (the same trap
  `dashboard/product_seo.py` documents).
* **GEO** — the specifications table, a FAQ whose answers stand alone, and
  `FAQPage` JSON-LD. `Product` JSON-LD is deliberately *not* emitted: Shopify
  themes already emit it, and a second, subtly different copy competes with
  the theme's rather than adding to it. Whether a `<script>` tag survives in a
  product description depends on the theme and on Shopify's own sanitising, so
  the same questions are written to a `custom.faq` metafield as well — if the
  tag is stripped, the visible FAQ still stands and a theme can render the
  markup from the metafield.
* **Local** — one availability line naming the service area, built from
  `BUSINESS_LOCATION` and the cities the local rank tracker already watches,
  so the product page and the dashboard mean the same thing by "our area".

## Settings

Settings → **Product import**:

| Setting | Default | Why you'd change it |
|---|---|---|
| Model that writes product copy | `~deepseek/deepseek-v4-flash-latest` | A stronger model writes better product pages; a rate-limited one falls back to plain copy. A name containing `/` routes through OpenRouter (needs `OPENROUTER_API_KEY` — check the exact id at [openrouter.ai/models](https://openrouter.ai/models), some carry a leading `~`); anything else routes through Google AI Studio (needs `GOOGLE_API_KEY`) |
| Most products from one collection | 60 | A ceiling in case the URL is the whole catalogue |
| Products handled per pass | 3 | Raise it when running locally — the 60-second function is what keeps it low |
| Most images per product | 8 | |
| Most documents per product | 4 | 0 turns off document handling entirely |
| Status new products are created with | `DRAFT` | `ACTIVE` publishes immediately, mistakes included |
| Tag added to every imported product | `imported` | How you find everything one import created, later |

## A range the store already carries

The form asks whether this is a new range or one you already carry, rather
than inferring it. Inferring gets the common case wrong in the expensive
direction: a range imported before its collection page was ever built looks
new from the store's side, and would get a second page.

Either way, nothing is removed:

- A product already in the store is **left exactly as it is** and is still
  added to the collection — it may exist without ever having been in one.
  (Unless you overrule that: see [Importing a skipped product
  anyway](#importing-a-skipped-product-anyway).)
- A product the supplier has since dropped **stays**. Their catalogue is a
  record of what they sell, not of what you do.
- An existing collection page is **added to, never rewritten**. It may have
  been edited by hand since it was made.

The run note says which is which — "3 new, 9 already in the store" — because
"12 products" reads the same whether all twelve are new or none are.

"Build the collection page if there isn't one yet" is separate, for an older
range whose page you maintain yourself. Untick it and the products are still
created and tagged, so a smart collection defined on the brand and
collection tags picks them up anyway.

## Importing a skipped product anyway

The skip is decided on the handle, and the handle is composed from the title.
So what it really answers is *"is there a product called this"*, not *"have we
imported this product"* — and those come apart in two ways, both of which end
with a product you asked for missing from the store:

- **Two source products compose to one name.** The maker distinguishes them in
  a way our naming drops, so the first is created and the second is reported
  as already yours when it was never imported at all.
- **The product on that handle is the right one but is wrong.** An early
  import with no documents, a run that died halfway, a description someone
  has since emptied. Leaving it untouched preserves the bad version.

The run page shows what was skipped and offers both ways out, per product or
for all of them at once. Which one it is cannot be worked out from here, so it
is not guessed:

| | What it does |
|---|---|
| **Rewrite the product already in the store** | Writes this run's title, description, tags, product type, vendor and SEO onto the product that is already there. Pictures are added **only if it has none of its own** — Shopify's media is additive, and re-attaching would leave it showing both sets. Documents are uploaded and linked as usual. |
| **Create it alongside, under a free handle** | Leaves the product in the store completely alone and creates this one next to it as `handle-2`. |

Either way the run reopens where it stands rather than starting again — the
range is one range, and a fresh run would cross-link the products it re-made
to each other instead of to their siblings. The rows go back to `pending`, the
run goes back to its products stage, and the ordinary passes carry it through
the collection and cross-linking stages from there: the run page advances it
while it is open, and the nightly job finishes it if you close the tab.

Cross-linking is redone for the **whole** range, not only the products you
overruled. A product whose copy has just changed is still described by its
siblings' grids as it was before, and those grids are only rewritten when the
product carrying them is marked unlinked.

Nothing is ever deleted, in either mode. A rewrite changes the fields listed
above and leaves everything else on the product — its variant, its price, its
inventory, its own images — as it was. A product that has been rewritten is
recorded as `updated` rather than `created`, so the run afterwards still says
which products it put in the store and which it changed that were already
there.

## Live on creation

Two switches, and a product needs both to be seen:

- **Status** — `ACTIVE` by default. The product is for sale the moment it is
  created.
- **Sales channels** — published to all of them by default: Online Store,
  Shop, Google & YouTube, Facebook & Instagram, Buy Button, whatever the
  store has.

They are genuinely separate: a product can be Active and still missing from
the Online Store, which is what makes "it's active, why can't I see it" a
real half-hour. Publishing a *draft* to channels is harmless — the status
still decides whether anyone can see it — so it happens on every create.

The previous default was `DRAFT` and no channel publishing at all, which
meant an import finished needing a second manual pass in Shopify admin to be
worth anything. That pass was easy to forget and left the catalogue
half-published. Both are settings if you want the old behaviour back.

**An import is now live to customers as it runs**, so a dry run first earns
its minutes — it is what stands between a bad extraction and the storefront.

## Product names

Every imported product is named to the store's standard:

```
Brand + Collection + Type + Size - Colour

EUROSTYLE Venice Grand PRO Waterproof Luxury Vinyl Plank - Bassano
Ames Tile 3D Bars Porcelain Wall Tile 5"x10" - Emerald Bevel Gloss
```

The size sits with the type because that is what it qualifies, and is left
out when the source doesn't state one rather than guessed at.

**The brand is required on the import form.** It used to say "left blank:
taken from the source", which is true of a Shopify supplier and false of
most others — a Magento catalogue publishes no brand anywhere a scraper can
read, so leaving it blank silently dropped the brand from every name in the
range.

Assembled by the app, not requested from the model. Three of the four parts
are things the import already knows — the vendor, the collection being
imported, the product type — so only the colour is read off the source, and
that is the single field the model contributes to the name. A format a model
is *asked* to follow is a format that holds for most of a catalogue.

Missing parts are dropped rather than leaving a gap, and a part already
contained in the next one is not repeated: a manufacturer publishing its
range as "EUROSTYLE Venice Grand PRO" under the brand "EUROSTYLE" still
produces one "EUROSTYLE".

**The size is the product's own, read off the maker's title and normalised.**
A range is routinely several sizes — Ames' "Advantage" is four colours in
24"x24", 24"x48" and 36"x72" — and where it is, the size is the only thing
separating one product in it from the next. Normalising is what makes that
safe: `5" x 10"`, `5"X10"` and `5"x10"` are one size and only one of them can
be the store's, so all three are written the same way, whether the answer
came from the source or from the model.

This was, briefly, the other way round — one size settled for the whole range
and applied to every product in it — and the cost was severe. Importing
Advantage, the first product settled 24"x24", all twelve composed the name of
the first four, and since the handle follows the name, eight of them found a
product on their handle and were logged as *"already in the store, left
untouched"*. Twelve products in, four out, and the run reported success. What
justified settling one size was two *spellings* of one size, which is
normalising's job, not the range's.

**Type and size still fall back to the range's** for a product whose page
states neither: it takes what the rest of the range answered rather than
going to the shelf with a gap. The first non-empty answer of the run is what
is kept. One residual: if the *first* product yields no type and a later one
does, the first keeps the shorter name — one product off-pattern beats nine,
and the alternative is holding every create until the whole range has been
read.

**Two products that compose one name are never reported as already yours.**
The last line of defence under all of the above, and it does not need to
guess: if the product sitting on the handle was put there by *this run* from
a different source page, then these are two different products by
construction, whatever the store looks like. The second goes in beside the
first under a free handle and the run log says exactly that — *"named the
same as X, so it was created as Y instead"*. Being told the naming collapsed
two products is recoverable; being told they were already yours is how a
range imports short and nobody finds out.

**When the source names no colour, the source's own title is used instead.**
The colour is the only part that separates one item in a range from the
next, so composing without it would give every product the same name — and
since the handle comes from the name, everything after the first would be
skipped as already in the store. A range silently importing as one product
is much worse than a name off-standard. When it happens anyway, the
collision rule above catches it inside the run, and the "create it alongside"
override catches it across runs.

## Collection names

A created collection is named to the store's standard:

```
Brand + Range + "Collection"
Cyrus Luxury Vinyl Collection      →  /collections/cyrus-luxury-vinyl-collection
Simba Engineered Flooring Collection
```

The handle follows from the title, which is what makes a re-import land on
the collection that already exists rather than beside it.

The brand is not repeated when the range name already carries it, and the
suffix is not doubled — a range published as "Craftsman Collection" stays
"Cyrus Craftsman Collection".

**This is deliberately not the collection tag.** The tag is what a smart
collection is defined on and what the "more from this range" heading reads,
so it stays the bare range name: "3D Bars", not "Ames Tile & Stone 3D Bars
Collection", which is a mouthful in a heading and unusable as a filter.

## Tags

Three tags are written by the app rather than left to the model, and they go
**first** in the list: the collection name, the brand (vendor) and the
flooring type, plus the `imported` source tag. Ordering is the point —
`clean_tags` caps a product at 20 tags and the model routinely proposes
fifteen, so tags appended after the model's would be the first ones dropped.

That guarantee is what makes a Shopify smart collection defined as *brand +
collection* work: it needs both tags on every product in the range every
time, not usually. A product the model happened to tag differently is a
product missing from its own collection page, with nothing about the page
saying so.

## The brand block, and linking your own categories

The block lives in Settings, and it accepts `<a href="...">` so the ranges it
names can be reached from every product page — naming them without linking
them is a dead end. Everything else is reduced to its text: paste from an
existing page and the editor's `<span>`, `<div>` and `<p>` wrappers are
unwrapped while the links survive.

That has to be a list of what's allowed rather than what isn't, because one
settings field reaches every product in the store. `javascript:` and `data:`
hrefs lose their link and keep their words; `<script>` is dropped with its
contents. Bare URLs still become links, so `www.drflooring.ca` works without
an anchor.

**Link Shopify collection handles, not WordPress paths.** The store's old
`/product-category/…` URLs do not exist on Shopify. The handles shipped by
default were read off the live store; check a new one against
`/collections/<handle>` before adding it.

## The description, top to bottom

1. **The call line** — "For SPECIAL prices, call us NOW at (604) 532-2211".
   First, because almost nothing in this catalogue is bought online and it is
   the only line on the page asking for the conversion the store has. Edit it
   in Settings.
2. Summary, description, key features, specifications, where to use it.
3. The local-availability line, then the **brand and offers block**.
4. Downloads — the manufacturer's PDFs, re-hosted on the store.
5. **The rest of the range**, each with a thumbnail and a link — every
   other product in the collection, whether this run created it or found it
   already in the store.

There is deliberately **no link to the manufacturer's page**. Sending a
ready-to-buy customer to the supplier is the one link a retailer should not
publish.

Layout for the range grid is written inline, not left to a class. This markup
ends up inside a Shopify product description rendered by a theme that has
never heard of this app, so `.related-grid` styles nothing — the block once
rendered as a bulleted list of full-width images for exactly that reason.
Only layout is set inline; colours and fonts are left to the theme.

### Re-importing rewrites descriptions

A product already in the store is left alone in every respect but one: its
description is rewritten so it carries the grid of the full range. It has to
be, or the range only links itself together for whichever products happened
to be new that day — and on a re-import that is most of them missing.

Titles, images and documents of an existing product are never touched by a
re-import on its own — only by an override you ask for, which rewrites the
whole product. The
run note says how many descriptions were rewritten, because "left untouched"
appears against those same products a minute earlier in the log.

## Reading a dry run before you commit to it

A dry run does the whole expensive path — scrape the page, download and read
the documents, call the model — and stops short of the create. All of that
copy is kept, so the run is reviewable without re-running anything.

Click a product's title on the run page. The preview shows the body produced
by the same `render_description()` the real create calls, so what's on
screen is what would be sent, not a summary of it — description, features,
specifications, applications and FAQs — plus the SEO title and description
with their character counts, and the tags including the `imported` one added
at create time.

Below that is what the model had to work with: which documents were read and
how much text came out of each, which specifications the source page itself
carried, and the images found. That pairing is the point. A thin description
is either a thin source or a PDF that failed to load, and those have
different fixes; counts alone can't tell them apart.

Two things are absent from a preview and present on a real create, both for
the same reason — a dry run uploads nothing:

- **Downloads.** With no store URLs to link, `render_downloads` omits a
  document rather than pointing at the manufacturer's copy. The documents
  that were read are listed separately instead.
- **Related products.** Cross-linking runs after every product in the
  collection exists.

Note that a real run does not reuse a dry run's work — it re-scrapes and
re-writes from scratch. The preview is where the decision gets made.

## When it goes wrong

**"No products found."** The collection page loaded but nothing on it looked
like a product link. Products are looked for three ways, best evidence
first: JSON-LD `ItemList`, then the grid's own markup (`.product-item`,
`.product-card`, `[data-container="product-grid"]`), then the shape of the
URL. The middle one is what makes a store that doesn't prefix its product
URLs work — Magento puts them at the site root, `/3dbbemg510` rather than
`/products/3dbbemg510`, and no pattern matching a bare slug could avoid also
matching `/about`. Asking the grid instead means the page has already
declared what's a product, so the link inside needs no prefix to be trusted.

If all three find nothing and `FIRECRAWL_API_KEY` is set, a
JavaScript-rendered fetch of the same URL is tried before this error is
raised; without it, try the manufacturer's sitemap or a different collection
URL.

**An import that stops one page early.** Paging follows whichever query
parameter the page's own pagination links use — `page`, or `p` for Magento.
Guessing this wrong is silent rather than loud, which is why it's read off
the page rather than assumed: Magento answers `?page=2` with HTTP 200 and
the *first* page, so a wrong guess looks like a successful fetch naming no
new products, and a 13-product collection imports 12 and reports success.

**Products created with three-sentence descriptions.** The source published
little and linked no documents. Check the dry run's extraction: if `specs` is
empty and `docs` is empty, there was nothing to work with.

**One product failed, the rest are fine.** That's the design. The reason is on
the row on the run page; the product can be re-imported by running the same
collection again (everything else will be skipped as already existing).

**A product was skipped that shouldn't have been.** The run page lists what it
skipped and lets you import it anyway, either over the product in the store or
beside it — see [Importing a skipped product
anyway](#importing-a-skipped-product-anyway).

**Images missing.** Shopify fetches image URLs itself; some manufacturer CDNs
block that. The importer notices and falls back to downloading each image and
uploading the bytes. If they're still missing, the source URLs are in the
run's extraction to check by hand.
