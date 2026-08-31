"""Mid-article conversion blocks.

An article that only asks for the sale at the very end asks a reader who has
already left. These blocks put the two things a convinced reader wants — a
phone number and something to buy — at section breaks partway down, where
interest peaks rather than where the text happens to stop:

  1. A **call banner** (~30% in) — the store's number as a `tel:` link, so a
     tap on a phone dials it. Rendered only when `business_phone` is set.
  2. A **featured product card** (~55% in) — one buyable product, picked by
     word overlap with the article's own keywords.
  3. A **collection card row** (~78% in) — two or three category cards, for
     the reader who wants to browse rather than buy one thing.

Everything here is deterministic: the catalogue comes from Shopify and the
choice of what to show is word overlap, so no LLM call is involved and the
same article always renders the same blocks.

Two deliberate constraints:

*Layout is inlined, never themed.* These blocks are injected into an article
body rendered by a theme we don't control and can't add a stylesheet to, so
every rule is an inline `style` attribute. A block that leans on a class the
theme has never heard of renders as unstyled stacked text.

*Links carry UTMs.* Each link is tagged with the article's slug, which is what
lets the GA4 job attribute a session to the post that earned it. A block whose
value can't be measured is a block nobody can argue for keeping. The `tel:`
link is the exception — a dialled call leaves no web trail, and measuring it
needs a click event in the theme, not a parameter here.
"""

from __future__ import annotations

import html
import math
import re

from blog_pipeline.config import _digits, get_settings
from blog_pipeline.utils import slugify, tokenize

# Fractions of the way through the body to aim each block at. Each one lands
# on the nearest section break, so these are targets rather than positions.
CALL_AT = 0.30
PRODUCT_AT = 0.55
COLLECTIONS_AT = 0.78

# How many cards a row shows at most. Three fits a desktop article column
# without wrapping and stacks cleanly on a phone.
PRODUCT_COUNT = 3
COLLECTION_COUNT = 3

# Neutral enough to sit inside somebody else's theme without clashing.
_INK = "#1a1a1a"
_MUTED = "#5c5c5c"
_LINE = "#e0dedb"
_WASH = "#f7f6f4"

_BLOCK = (
    f"margin:2em 0;padding:1.25em 1.4em;background:{_WASH};"
    f"border:1px solid {_LINE};border-radius:10px"
)
_BUTTON = (
    f"display:inline-block;margin-top:.7em;padding:.7em 1.3em;background:{_INK};"
    "color:#ffffff;text-decoration:none;border-radius:6px;font-weight:600"
)
_HEADING = f"margin:0 0 .35em;font-size:1.15em;font-weight:700;color:{_INK}"
_SUB = f"margin:0;color:{_MUTED};font-size:.95em"
_CARD = (
    f"flex:1 1 180px;min-width:0;border:1px solid {_LINE};border-radius:10px;"
    "overflow:hidden;background:#ffffff;text-decoration:none;display:block"
)
_CARD_IMG = "display:block;width:100%;height:150px;object-fit:cover"
_ROW = "display:flex;flex-wrap:wrap;gap:1em;margin:2em 0"


def _esc(text: str) -> str:
    return html.escape((text or "").strip())


def tag_url(url: str, medium: str, campaign: str) -> str:
    """Add campaign tracking so GA4 can tell which post drove the session."""
    if not url:
        return url
    params = f"utm_source=blog&utm_medium={medium}&utm_campaign={slugify(campaign)}"
    return f"{url}{'&' if '?' in url else '?'}{params}"


def render_call_banner() -> str:
    """The store's phone number as a tappable banner, or '' when unset."""
    settings = get_settings()
    if not settings.has_call_cta:
        return ""
    number = _esc(settings.business_phone)
    digits = _digits(settings.business_phone)
    # A leading + is meaningful to a dialler and is lost with the punctuation.
    href = f"+{digits}" if settings.business_phone.strip().startswith("+") else digits
    name = _esc(settings.business_name) or "our team"
    hours = _esc(settings.business_hours)
    hours_line = f'<p style="{_SUB}">{hours}</p>' if hours else ""
    return (
        f'<div class="call-banner" style="{_BLOCK}">'
        f'<p style="{_HEADING}">Not sure which floor is right for your room?</p>'
        f'<p style="{_SUB}">Talk it through with {name} — measurements, '
        "subfloor, budget, and what actually holds up in your space.</p>"
        f'<a href="tel:{href}" style="{_BUTTON}">Call {number}</a>'
        f"{hours_line}</div>"
    )


def _render_cards(
    items: list[dict], *, medium: str, campaign: str, css_class: str, label: str
) -> str:
    """A row of picture-and-name cards linking into the store."""
    cards = []
    for item in items:
        title = _esc(item.get("title", ""))
        if not title or not item.get("url") or not item.get("image"):
            continue
        price = _esc(item.get("price", ""))
        foot = (
            f'<p style="margin:.2em 0 0;color:{_INK};font-weight:600">{price}</p>'
            if price
            else f'<p style="{_SUB};margin-top:.2em">{label}</p>'
        )
        href = tag_url(item["url"], medium, campaign)
        cards.append(
            f'<a href="{href}" style="{_CARD}">'
            f'<img src="{_esc(item["image"])}" alt="{title}" style="{_CARD_IMG}" '
            'loading="lazy">'
            '<div style="padding:.7em .85em">'
            f'<p style="margin:0;font-weight:600;color:{_INK};line-height:1.3">'
            f"{title}</p>{foot}</div></a>"
        )
    if not cards:
        return ""
    return f'<div class="{css_class}" style="{_ROW}">{"".join(cards)}</div>'


def render_product_row(products: list[dict], campaign: str) -> str:
    """Matching products as a row of cards.

    A row rather than the single hero card this replaced: a reader convinced
    by an article about herringbone wants to see the herringbone range, and
    one product is a suggestion where three are a choice.
    """
    return _render_cards(
        products, medium="product-card", campaign=campaign,
        css_class="product-cards", label="View product &rarr;",
    )


def render_collection_row(collections: list[dict], campaign: str) -> str:
    """A row of category cards for the reader who would rather browse."""
    return _render_cards(
        collections, medium="collection-card", campaign=campaign,
        css_class="collection-cards", label="Shop the range &rarr;",
    )


# Words that match everything in a flooring catalogue and so distinguish
# nothing — scoring on them ranks the whole store equally.
_STOPWORDS = {
    # grammar
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with",
    "your", "you", "is", "are", "how", "what", "which", "why", "when",
    # the vocabulary of article titles — present in half the blog and in
    # nothing a reader wants to buy
    "best", "guide", "ultimate", "comprehensive", "essential", "complete",
    "must", "need", "know", "top", "expert", "tip", "tips", "choose",
    "choosing", "transform", "modern", "trend", "trends", "option",
    "options", "design", "home", "new", "everything",
    # true of the entire catalogue, so it separates nothing
    "flooring", "floor", "floors",
    # the service area. These appear in the store's location-marketing tags
    # ("Best Laminate Flooring Langley"), which would otherwise let any
    # product match any article that names a place. Wood species that double
    # as place names (maple, oak) are deliberately not here.
    "british", "columbia", "langley", "surrey", "vancouver", "abbotsford",
    "canada", "canadian",
}


def _stems(text: str) -> set[str]:
    """Word stems, crudely. Enough that "ceramics" matches "ceramic" and
    "laminated" matches "laminate" — exact tokens don't, which is why the
    first pass at this ranked every collection at zero and fell back to
    alphabetical order for the whole catalogue."""
    out = set()
    for token in tokenize(text):
        if token in _STOPWORDS or len(token) < 3:
            continue
        for suffix in ("ings", "ing", "es", "s"):
            if len(token) > len(suffix) + 2 and token.endswith(suffix):
                token = token[: -len(suffix)]
                break
        out.add(token)
    return out


def _candidate_text(candidate: dict) -> str:
    """What a catalogue entry should be matched on — its `match_text` when the
    Shopify read supplied one, otherwise just its title."""
    return candidate.get("match_text") or candidate.get("title") or ""


def build_idf(candidates: list[dict]) -> dict[str, float]:
    """Weight each word by how rare it is in this catalogue.

    Without this, "flooring" counts as much as "herringbone" in a flooring
    store, so an article about herringbone matches everything equally and the
    tie is broken alphabetically. Rarity is the whole signal: 34 products say
    herringbone and 2,396 say flooring, so herringbone should be worth far
    more. Shaped as log(1 + N/(1+df)) rather than the textbook log(N/df) so a
    word present in every document scores small but never negative.
    """
    df: dict[str, int] = {}
    for candidate in candidates:
        for stem in _stems(_candidate_text(candidate)):
            df[stem] = df.get(stem, 0) + 1
    total = len(candidates) or 1
    return {stem: math.log(1 + total / (1 + n)) for stem, n in df.items()}


def match_score(subject: str, text: str, idf: dict[str, float]) -> float:
    """Total rarity-weight of the words an article and a product share.

    Summed, not averaged over the article's words: a title like "Herringbone
    Flooring Trends in British Columbia" has plenty of words no product will
    ever match, and dividing by all of them buried a perfect herringbone hit
    beneath generic planks that happened to match "Columbia" through a
    location tag.
    """
    shared = {s for s in _stems(subject) if s not in _STOPWORDS} & _stems(text)
    return sum(idf.get(stem, 0.0) for stem in shared)


def pick_best(candidates: list[dict], keywords: list[str], count: int = 1) -> list[dict]:
    """The `count` catalogue entries that best answer the article's keywords.

    Only entries that actually share something with the article are returned,
    so a row may come back short or empty. That is deliberate: the earlier
    version padded to `count` from the top of the catalogue, which is how an
    article about hardwood and ceramics ended up advertising Clearance.
    """
    if not candidates:
        return []
    subject = " ".join(k for k in keywords if k)
    if not subject:
        return candidates[:count]
    idf = build_idf(candidates)
    scored = [
        (match_score(subject, _candidate_text(c), idf), c.get("title") or "", c)
        for c in candidates
    ]
    # Title, not catalogue position, breaks a tie. Whole ranges score
    # identically here — eleven herringbone planks differ only by colour — and
    # Shopify does not guarantee the same page order twice, so ranking by
    # position made a rebuild pick a different product each run and every
    # --replace rewrote posts that had not actually changed.
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [c for score, _, c in scored[:count] if score > 0]


def strip_blocks(body_html: str) -> str:
    """Remove every block this module injected, nested markup and all.

    What makes re-running possible: placement and matching will keep
    improving, and without this the first version injected is the version a
    post keeps forever. Walks div depth rather than regex-matching a closing
    tag, because the product card nests one.
    """
    for match in reversed(list(_ANY_BLOCK_RE.finditer(body_html))):
        depth, i, end = 0, match.start(), None
        while i < len(body_html):
            nxt_open = body_html.find("<div", i)
            nxt_close = body_html.find("</div>", i)
            if nxt_close == -1:
                break
            if nxt_open != -1 and nxt_open < nxt_close:
                depth += 1
                i = nxt_open + 4
            else:
                depth -= 1
                i = nxt_close + 6
                if depth == 0:
                    end = i
                    break
        if end is not None:
            body_html = body_html[: match.start()] + body_html[end:]
    return body_html


# One marker per block, so "has this post got a banner?" is answerable
# separately from "has it got cards?". An all-or-nothing check would mean a
# post that got cards before the phone number was configured could never be
# topped up with a banner afterwards.
BLOCK_MARKERS = {
    "call": '<div class="call-banner"',
    "product": '<div class="product-cards"',
    "collections": '<div class="collection-cards"',
}

# `product-cards?` matches both: posts injected before the single hero card
# became a row still carry the old class and must strip cleanly.
_ANY_BLOCK_RE = re.compile(
    r'<div class="(?:call-banner|product-cards?|collection-cards)"'
)


def blocks_present(body_html: str) -> set[str]:
    """Which blocks this body already carries."""
    return {name for name, mark in BLOCK_MARKERS.items() if mark in body_html}


def _occupied_breaks(body_html: str) -> set[int]:
    """Section breaks that already have one of our blocks sitting before them.

    A block is always inserted immediately before an `<h2>`, so the heading
    that follows an existing block is the break it occupies. Excluding those
    is what stops a top-up run from stacking a new banner against a card
    placed on an earlier run.
    """
    occupied: set[int] = set()
    for match in _ANY_BLOCK_RE.finditer(body_html):
        nxt = re.search(r"<h2", body_html[match.end():], re.I)
        if nxt:
            occupied.add(match.end() + nxt.start())
    return occupied


def _section_breaks(body_html: str) -> list[int]:
    """Offsets of the `<h2>` boundaries a block may be inserted at.

    The first heading is skipped: it's where the intro ends and where the GEO
    pass puts its takeaways box, and a sales block wedged into that opening
    run reads as an advert before the article has said anything.
    """
    return [m.start() for m in re.finditer(r"<h2", body_html, re.I)][1:]


def place_blocks(body_html: str, blocks: list[tuple[float, str]]) -> str:
    """Insert each (target fraction, html) block at its nearest section break.

    Blocks never share a break and never split a paragraph. When there aren't
    enough breaks to go round, the later blocks are dropped — three adverts
    stacked in one gap convert worse than one in the right place.
    """
    if not body_html:
        return body_html
    occupied = _occupied_breaks(body_html)
    breaks = [b for b in _section_breaks(body_html) if b not in occupied]
    if not breaks:
        return body_html
    total = len(body_html)
    taken: dict[int, str] = {}
    for fraction, block in blocks:
        if not block:
            continue
        free = [b for b in breaks if b not in taken]
        if not free:
            break
        target = total * fraction
        taken[min(free, key=lambda b: abs(b - target))] = block
    # Late to early, so an insertion never shifts an offset still to be used.
    for offset in sorted(taken, reverse=True):
        body_html = body_html[:offset] + taken[offset] + body_html[offset:]
    return body_html


def apply_conversion_blocks(
    *,
    body_html: str,
    campaign: str,
    keywords: list[str] | None = None,
    products: list[dict] | None = None,
    collections: list[dict] | None = None,
    skip: set[str] | None = None,
) -> str:
    """Return body_html with a call banner, a product card, and a collection
    row placed at section breaks. Each block is skipped when the settings or
    catalogue it needs are missing, so this is safe to call unconditionally.

    `skip` names blocks to leave out — pass `blocks_present(body)` to top up
    a post that already has some of them without duplicating those.
    """
    if not get_settings().enable_conversion_blocks:
        return body_html
    keywords = keywords or []
    skip = skip or set()
    best_product = pick_best(products or [], keywords, count=PRODUCT_COUNT)
    best_collections = pick_best(collections or [], keywords, count=COLLECTION_COUNT)
    return place_blocks(
        body_html,
        [
            (CALL_AT, "" if "call" in skip else render_call_banner()),
            (
                PRODUCT_AT,
                ""
                if "product" in skip
                else render_product_row(best_product, campaign),
            ),
            (
                COLLECTIONS_AT,
                ""
                if "collections" in skip
                else render_collection_row(best_collections, campaign),
            ),
        ],
    )
