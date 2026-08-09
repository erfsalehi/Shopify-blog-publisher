"""Print the products worth tagging `show-price` first.

`python -m dashboard.tools.show_price_candidates`

Reads the catalogue snapshot, so run the Shopify sync first if it looks
stale. See docs/show-price.md for what to do with the list.
"""

from __future__ import annotations

import argparse
import sys

from dashboard.db import get_session
from dashboard.models import ShopifyProduct

# Commodity accessories: the items where a visible price wins the click,
# because nobody phones a flooring store to ask what a $20 reducer costs.
#
# `mould` / `mold` are matched as bare stems, not as "moulding". The catalogue
# spells them "T-Mould", which "moulding" does not contain — an earlier
# version of this list missed 21 products that way, and a filter that quietly
# under-reports is worse than no filter.
ACCESSORY_WORDS = (
    "underlay", "underlayment", "stair nose", "nosing", "transition",
    "mould", "mold", "reducer", "quarter round", "baseboard",
    "trim", "adhesive", "leveler", "levelling", "leveling",
)

# An accessory priced below this is not a per-piece price — it's a
# per-square-foot figure or a data entry error. Publishing it as a product
# price would be wrong in public. Flagged rather than silently dropped,
# because the fix belongs in Shopify.
SUSPICIOUS_UNDER = 5.00


def is_accessory(product: ShopifyProduct) -> bool:
    return any(word in (product.title or "").lower() for word in ACCESSORY_WORDS)


def candidates() -> dict[str, list[ShopifyProduct]]:
    """Priced products, split by whether their price can be published as-is.

    The split is by *what the product is*, not by how cheap it is. Accessories
    sell per piece, so their price is the price. Flooring sells per square
    foot, so its price is a rate — and none of these products has a Shopify
    unit-price measurement set, meaning nothing in the store data says so.
    Publishing `"price": 1.09` for a laminate floor would state something the
    customer will not pay.
    """
    with get_session() as session:
        rows = session.query(ShopifyProduct).all()
        for row in rows:
            session.expunge(row)
    priced = [p for p in rows if p.price_min > 0]
    accessories = [p for p in priced if is_accessory(p)]
    return {
        "ready": sorted(
            (p for p in accessories if p.price_min >= SUSPICIOUS_UNDER),
            key=lambda p: (p.price_min, p.title),
        ),
        "bad_price": sorted(
            (p for p in accessories if p.price_min < SUSPICIOUS_UNDER),
            key=lambda p: p.price_min,
        ),
        "per_unit": sorted(
            (p for p in priced if not is_accessory(p)),
            key=lambda p: (p.price_min, p.title),
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--handles", action="store_true",
        help="Print bare handles only, for piping into a bulk edit.",
    )
    parser.add_argument(
        "--per-unit", action="store_true",
        help="Also list the per-square-foot flooring that must not be tagged.",
    )
    args = parser.parse_args(argv)

    groups = candidates()
    if args.handles:
        for product in groups["ready"]:
            print(product.handle)
        return 0

    print(f"{len(groups['ready'])} products READY to tag `show-price` "
          f"(already priced - no admin entry needed)\n")
    for product in groups["ready"]:
        print(f"  ${product.price_min:>7.2f}  {product.title[:56]:<58} "
              f"{product.handle}")

    if groups["bad_price"]:
        print(f"\n{len(groups['bad_price'])} DO NOT TAG - the price is not a "
              f"per-piece price:")
        for product in groups["bad_price"]:
            print(f"  ${product.price_min:>7.2f}  {product.title}")
        print("  Fix the price in Shopify, or leave untagged.")

    if groups["per_unit"]:
        print(f"\n{len(groups['per_unit'])} DO NOT TAG - flooring priced per "
              f"square foot:")
        lo = min(p.price_min for p in groups["per_unit"])
        hi = max(p.price_min for p in groups["per_unit"])
        print(f"  ${lo:.2f} - ${hi:.2f}, {len(groups['per_unit'])} products.")
        print("  None has a Shopify unit-price measurement set, so nothing in")
        print("  the store data says these are rates. Tagging them would")
        print("  publish e.g. \"$1.09\" as the price of a floor.")
        print("  Use --per-unit to list them.")

    if args.per_unit:
        print()
        for product in groups["per_unit"]:
            print(f"  ${product.price_min:>7.2f}  {product.title[:56]:<58} "
                  f"{product.handle}")

    print("\nNext: docs/show-price.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
