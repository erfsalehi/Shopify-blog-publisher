# The `show-price` mechanism

Prerequisite for Phase 3 price experiments, and useful on its own: a visible
price wins clicks on commodity accessories that people don't phone about.

One tag — `show-price` — drives **both** the Orichi hide-price app's exclusion
list **and** the theme's JSON-LD `offers` block. That is the whole design.
Two separate switches would eventually disagree, and the day they do, the page
says "Call for price" while the markup says $20, which is exactly the
fabricated structured data this project refuses to ship.

The rule that must never break: **markup matches the visible page.**

---

## Step 1 — Owner: Orichi app

In the Orichi hide-price app's settings, find `excludeProductTags` (its
config already supports it; it is currently off/empty) and add:

```
show-price
```

Products carrying that tag then show their real price and their normal Add to
cart button. Everything else keeps today's "Call for price" →
`tel:+16045322211` behaviour, unchanged.

Do this **before** step 3. If the theme starts emitting `offers` while Orichi
is still hiding the price, the markup and the page disagree — briefly, but in
the direction that gets a manual action.

## Step 2 — Owner: tag the first product set

Recommended first set is below. Tag in Shopify admin (bulk edit → Tags), or
from a collection view.

## Step 3 — Theme patch

`Online Store → Themes → Refresh Aug 2025 → Edit code →
sections/main-product.liquid`.

**Duplicate the theme first and edit the copy**, preview it, then publish. This
file is 100,729 characters and a mis-paste is a broken product page on a live
store.

Find the block that begins:

```liquid
{%- comment -%}
    Product JSON-LD, emitted ONLY when the product has real reviews.
```

…and ends with `</script>` / `{%- endif -%}` just before `</div>`
`</product-info>`. Replace that whole block — comment, `{%- liquid -%}`
assigns, `{%- if -%}`, script, `{%- endif -%}` — with:

```liquid
{%- comment -%}
    Product JSON-LD.

    Two independent gates. Either one satisfies Google's Product rich result,
    which needs one of offers / review / aggregateRating — and neither may
    ever state something the visible page doesn't:

      * aggregateRating — when the product has real reviews.
      * offers — ONLY for products tagged `show-price` that have a real
        price. Everything else hides its price behind the Orichi app
        ("Call for price"), so an offers block would contradict the page:
          offers + price 0.0 -> contradicts "Call for price"
          offers, no price   -> critical "price should be specified"

    The `show-price` tag drives both this markup and Orichi's exclusion list,
    so the two cannot drift: one tag, one truth.

    A product with neither reviews nor a visible price gets no Product markup.
    That is not an error — it just means no product rich result, and
    structured data is not a ranking signal. Tiny SEO's
    product-json-ld-embed stays disabled because it always writes offers.
  {%- endcomment -%}
  {%- liquid
    assign r = product.metafields.reviews.rating.value
    assign rc = product.metafields.reviews.rating_count.value
    assign has_reviews = false
    if r != blank and rc > 0
      assign has_reviews = true
    endif

    assign show_price = false
    if product.tags contains 'show-price' and product.price_min > 0
      assign show_price = true
    endif
  -%}
  {%- if has_reviews or show_price -%}
  <script type="application/ld+json">
    {
      "@context": "https://schema.org/",
      "@type": "Product",
      "@id": {{ shop.url | append: product.url | json }},
      {%- if seo_media %}
      "image": [{{ seo_media | image_url: width: 1445 | prepend: "https:" | json }}],
      {%- endif %}
      {%- if product.description != blank %}
      "description": {{ product.description | strip_html | truncate: 400 | json }},
      {%- endif %}
      {%- if product.vendor != blank %}
      "brand": { "@type": "Brand", "name": {{ product.vendor | json }} },
      {%- endif %}
      {%- if product.selected_or_first_available_variant.sku != blank %}
      "sku": {{ product.selected_or_first_available_variant.sku | json }},
      {%- endif %}
      {%- if show_price -%}
      {%- comment -%}
        availability comes from product.available, NOT from an inventory
        count. This store has inventory tracking switched off, so every
        quantity reads 0 while the page shows an Add to cart button —
        quoting the count would mark every product OutOfStock, which is
        worse than emitting no markup at all.
      {%- endcomment -%}
      {%- if product.price_min == product.price_max %}
      "offers": {
        "@type": "Offer",
        "url": {{ shop.url | append: product.url | json }},
        "price": {{ product.price_min | divided_by: 100.0 | json }},
        "priceCurrency": {{ cart.currency.iso_code | json }},
        "availability": {% if product.available %}"https://schema.org/InStock"{% else %}"https://schema.org/OutOfStock"{% endif %}
      },
      {%- else %}
      "offers": {
        "@type": "AggregateOffer",
        "url": {{ shop.url | append: product.url | json }},
        "lowPrice": {{ product.price_min | divided_by: 100.0 | json }},
        "highPrice": {{ product.price_max | divided_by: 100.0 | json }},
        "priceCurrency": {{ cart.currency.iso_code | json }},
        "offerCount": {{ product.variants.size | json }},
        "availability": {% if product.available %}"https://schema.org/InStock"{% else %}"https://schema.org/OutOfStock"{% endif %}
      },
      {%- endif %}
      {%- endif %}
      {%- if has_reviews %}
      "aggregateRating": {
        "@type": "AggregateRating",
        "ratingValue": {{ r.rating | json }},
        "bestRating": {{ r.scale_max | json }},
        "worstRating": {{ r.scale_min | json }},
        "ratingCount": {{ rc | json }}
      },
      {%- endif %}
      "url": {{ shop.url | append: product.url | json }},
      "name": {{ product.title | json }}
    }
  </script>
  {%- endif -%}
```

### What changed, exactly

| | Before | After |
|---|---|---|
| Untagged product, no reviews | no markup | **no markup** (unchanged) |
| Untagged product, has reviews | `aggregateRating` | **`aggregateRating`** (unchanged) |
| `show-price` product, no reviews | no markup | `offers` |
| `show-price` product, has reviews | `aggregateRating` | `offers` + `aggregateRating` |

Nothing an untagged product emits changes. That is deliberate: 2,718 of 2,786
products start untagged, and the patch should be a no-op for all of them.

### Two details that are easy to get wrong

- **Price is in cents.** `product.price_min | divided_by: 100.0` → `30.0`.
  Emitting `product.price_min` raw publishes `3000`.
- **Variant ranges need `AggregateOffer`**, not `Offer` with one price — the
  page shows "From $12.00" and a single `price` would contradict it. None of
  the 68 recommended products currently has a range, but a variant added
  later would silently create one, so the branch is there.

## Step 4 — Validate before publishing the theme

On the theme preview, for one tagged product and one untagged product:

1. **View source**, find the `application/ld+json` block. Confirm the tagged
   one has `offers` with the same number the page displays, and the untagged
   one is unchanged.
2. Paste the URL into Google's
   [Rich Results Test](https://search.google.com/test/rich-results). Expect
   "Product snippets" valid. A `priceValidUntil` *warning* is fine and is
   deliberately not faked.
3. Confirm the tagged product's page shows a real price and Add to cart, and
   the untagged one still shows "Call for price".

Then publish. Re-run the Rich Results Test on the live URL once.

---

## The recommended first set

**68 accessories** already carry a real per-piece price, so they need **no
admin price entry** — only the tag. Reducers, stair noses and T-Mouldings at
$12–$30: exactly the commodity items where a visible number wins the click,
because nobody phones a flooring store to ask what a $20 reducer costs.

| Price | Count |
|---|---|
| $12.00 | 6 |
| $14.00 | 15 |
| $15.00 | 8 |
| $16.00 | 8 |
| $20.00 | 18 |
| $24.00 | 4 |
| $25.00 | 2 |
| $30.00 | 7 |

Get the list any time:

```bash
python -m dashboard.tools.show_price_candidates
```

`--handles` prints bare handles for a bulk edit.

### 25 priced products that must NOT be tagged

93 products in the catalogue have a non-zero price. 68 are ready; the other 25
would publish something untrue.

**24 are flooring priced per square foot** — 15 laminates at $1.09–$2.59, 9
Dansk Engineered at $6.35. Verified against Shopify: **none has a
`unitPriceMeasurement` set**, so nothing in the store data marks these as
rates. Tagging one would put `"price": 1.09` in the markup and "$1.09" on the
page as the price of a floor, which is not what a customer pays.

To sell these with a visible price honestly, either set Shopify's unit price
measurement on the variants and extend the liquid to emit a schema.org
`UnitPriceSpecification` with the right `unitCode`, or keep them on "Call for
price". Out of scope for the first rollout either way — the accessories are
where the win is.

**1 is a data error**: `Underlay - Memory Foam Vinyl Underlayment` at $0.39.
Fix the price in Shopify or leave it untagged.

List them with `--per-unit`.

### Why accessories first

They are the products where a visible price genuinely wins the click: nobody
phones a flooring store to ask what a $20 reducer costs, they just buy from
whoever shows a number. The main catalogue is a different decision — those are
quoted jobs where the phone call *is* the conversion — and nothing here
commits you to it.

---

## Then: Phase 3

Once tagged products are live and showing prices, the experiments feature can
run a real price test on them: `/experiments` in the dashboard. Until then
title and description experiments work on any product, priced or not.
