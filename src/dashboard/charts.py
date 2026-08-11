"""Server-rendered inline SVG charts.

No charting library. Not to be clever — because the alternative was vendoring
a JavaScript bundle into a repo on a machine whose outbound requests fail at
random, to draw a line through thirty numbers. SVG generated here renders with
no network, no build step and no client JS, prints correctly, and inherits the
page's own colours in both themes.

The one thing this must get right is the provisional tail. Search Console
restates its last few days, so the final points of every trend are guaranteed
to tick downward. Drawing them identically to settled data would show the
owner a decline that isn't happening, every single day. They render dashed and
muted, with the boundary marked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from html import escape

from markupsafe import Markup

# Chart geometry. The SVG scales to its container via viewBox, so these are
# aspect-ratio units rather than pixels — and since the aspect ratio is
# preserved, their ratio is what sets the rendered height. Roughly 5:1 keeps a
# 90-day trend readable without two stacked charts filling the screen.
_W = 960
_H = 200
_PAD_L = 52
_PAD_R = 10
_PAD_T = 12
_PAD_B = 26


@dataclass(frozen=True)
class Point:
    label: str          # x-axis label, e.g. an ISO date
    value: float
    provisional: bool = False
    tooltip: str | None = None


def _nice_ceiling(value: float) -> float:
    """Round a max up to something a human would pick for an axis."""
    if value <= 0:
        return 1.0
    magnitude = 10 ** (len(str(int(value))) - 1)
    for step in (1, 2, 2.5, 5, 10):
        candidate = magnitude * step
        if candidate >= value:
            return float(candidate)
    return float(magnitude * 10)


def _fmt(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 1_000:
        return f"{value / 1_000:.1f}k".replace(".0k", "k")
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}"


def line_chart(points: list[Point], *, title: str = "", empty_note: str = "No data yet") -> Markup:
    if not points:
        return Markup(
            f'<p class="chart-empty">{escape(empty_note)}</p>'
        )

    top = _nice_ceiling(max(p.value for p in points))
    plot_w = _W - _PAD_L - _PAD_R
    plot_h = _H - _PAD_T - _PAD_B
    n = len(points)

    def x_of(i: int) -> float:
        if n == 1:
            return _PAD_L + plot_w / 2
        return _PAD_L + plot_w * i / (n - 1)

    def y_of(value: float) -> float:
        return _PAD_T + plot_h * (1 - (value / top if top else 0))

    coords = [(x_of(i), y_of(p.value)) for i, p in enumerate(points)]
    first_provisional = next(
        (i for i, p in enumerate(points) if p.provisional), None
    )
    settled_end = n if first_provisional is None else first_provisional

    def path(pairs) -> str:
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in pairs)

    parts: list[str] = [
        # Aspect ratio is preserved deliberately. Stretching the viewBox to
        # fill a fixed-height container distorts the <text> glyphs along with
        # everything else — the axis labels come out visibly widened, which is
        # the one part of the chart that has to stay legible.
        f'<svg class="chart" viewBox="0 0 {_W} {_H}" role="img" '
        f'aria-label="{escape(title or "trend")}">'
    ]

    # Horizontal gridlines with value labels: 0, half, top.
    for fraction in (0.0, 0.5, 1.0):
        value = top * fraction
        y = y_of(value)
        parts.append(
            f'<line class="chart-grid" x1="{_PAD_L}" y1="{y:.1f}" '
            f'x2="{_W - _PAD_R}" y2="{y:.1f}" />'
        )
        parts.append(
            f'<text class="chart-axis" x="{_PAD_L - 6}" y="{y + 3.5:.1f}" '
            f'text-anchor="end">{escape(_fmt(value))}</text>'
        )

    settled = coords[:settled_end]
    if settled:
        # Area under the settled line only — filling under provisional data
        # would give the uncertain part the most visual weight on the chart.
        area = (
            f'{_PAD_L},{y_of(0):.1f} '
            + path(settled)
            + f' {settled[-1][0]:.1f},{y_of(0):.1f}'
        )
        parts.append(f'<polygon class="chart-area" points="{area}" />')
        parts.append(f'<polyline class="chart-line" points="{path(settled)}" />')

    if first_provisional is not None:
        # Start the dashed run at the last settled point so the line connects.
        tail = coords[max(settled_end - 1, 0):]
        parts.append(
            f'<polyline class="chart-line chart-line-provisional" '
            f'points="{path(tail)}" />'
        )
        boundary = coords[max(settled_end - 1, 0)][0]
        parts.append(
            f'<line class="chart-boundary" x1="{boundary:.1f}" y1="{_PAD_T}" '
            f'x2="{boundary:.1f}" y2="{_PAD_T + plot_h}" />'
        )

    # x labels: first, middle, last — more than three collide at this width.
    for i in {0, n // 2, n - 1}:
        x = x_of(i)
        anchor = "start" if i == 0 else "end" if i == n - 1 else "middle"
        parts.append(
            f'<text class="chart-axis" x="{x:.1f}" y="{_H - 8}" '
            f'text-anchor="{anchor}">{escape(points[i].label)}</text>'
        )

    # Hover targets. A full-height rect per point gives a tooltip anywhere in
    # the column, which is how a chart is expected to behave, using nothing
    # but SVG's native <title>.
    band = plot_w / max(n - 1, 1)
    for i, (x, y) in enumerate(coords):
        p = points[i]
        tip = p.tooltip or f"{p.label}: {_fmt(p.value)}"
        if p.provisional:
            tip += " (provisional)"
        parts.append(
            f'<g class="chart-hit"><rect x="{x - band / 2:.1f}" y="{_PAD_T}" '
            f'width="{band:.1f}" height="{plot_h:.1f}" />'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" />'
            f'<title>{escape(tip)}</title></g>'
        )

    parts.append("</svg>")
    return Markup("".join(parts))


# ── Pipeline funnel ─────────────────────────────────────────────────

_FUNNEL_W = 960
_FUNNEL_H = 132
_FUNNEL_GAP = 10

#: Which stages need the owner to do something. Coloured as attention rather
#: than as failure: a Shopify draft isn't broken, it's finished work sitting
#: one click from being live, which is a different feeling and should look
#: like one.
_ACTIONABLE = {"held", "shopify_draft", "stranded"}


def pipeline_funnel(counts: dict, stages) -> Markup:
    """Where every article is, as one row of proportional blocks.

    A funnel rather than a bar chart because the stages are sequential and
    the question is "where is the pile", not "compare these six numbers".
    Blocks are sized by count with a floor, so a stage holding one article is
    still clickable and still visible next to a stage holding seventy — the
    single stuck article is usually the one worth seeing.

    Empty stages are drawn as a thin marker rather than dropped, because the
    shape of the pipeline is the message: a gap at "Queued" means nothing is
    coming, and a stage that vanished couldn't tell you that.
    """
    present = [(key, label, note) for key, label, note in stages]
    total = sum(max(0, counts.get(key, 0)) for key, _, _ in present) or 1

    # Every block gets a minimum share so small stages stay legible; the rest
    # of the width is distributed by actual count.
    floor = 0.055
    weights = []
    for key, _, _ in present:
        share = max(0, counts.get(key, 0)) / total
        weights.append(floor + share * (1 - floor * len(present)))
    scale = sum(weights) or 1
    widths = [
        w / scale * (_FUNNEL_W - _FUNNEL_GAP * (len(present) - 1)) for w in weights
    ]

    parts = [
        f'<svg viewBox="0 0 {_FUNNEL_W} {_FUNNEL_H}" class="funnel" '
        f'role="img" aria-label="Where every article is in the pipeline">'
    ]
    x = 0.0
    for (key, label, note), width in zip(present, widths):
        count = max(0, counts.get(key, 0))
        empty = count == 0
        css = "funnel-block"
        if key == "published":
            css += " done"
        elif empty:
            css += " empty"
        elif key in _ACTIONABLE:
            css += " act"

        parts.append(
            f'<g class="{css}">'
            f'<rect x="{x:.1f}" y="0" width="{width:.1f}" height="46" rx="6"/>'
            f'<text x="{x + width / 2:.1f}" y="30" class="funnel-count" '
            f'text-anchor="middle">{count}</text>'
            f'<text x="{x + width / 2:.1f}" y="72" class="funnel-label" '
            f'text-anchor="middle">{escape(label)}</text>'
            f'<text x="{x + width / 2:.1f}" y="90" class="funnel-note" '
            f'text-anchor="middle">{escape(_wrap(note))}</text>'
            f"</g>"
        )
        x += width + _FUNNEL_GAP
    parts.append("</svg>")
    return Markup("".join(parts))


def _wrap(note: str, limit: int = 30) -> str:
    """SVG text doesn't wrap. Truncate rather than overflow into the
    neighbouring block, where it would read as that block's label."""
    return note if len(note) <= limit else note[: limit - 1] + "…"


# ── Price history: ours against theirs ──────────────────────────────

_PRICE_W = 720
_PRICE_H = 190


def price_chart(
    ours: list[tuple[date, float]],
    theirs: list[tuple[date, float]],
    *,
    our_label: str = "Ours",
    their_label: str = "Theirs",
) -> Markup:
    """Two price lines over the same dates.

    Separate from `line_chart` rather than an option on it: that one draws a
    single series with a provisional tail, and the interesting thing here is
    the *gap* between two series — which needs a shared y-axis anchored so
    the comparison is honest.

    The y-axis deliberately does **not** start at zero. Prices here live in a
    narrow band ($2–6/sqft) and a zero-based axis flattens both lines into
    one indistinguishable strip, hiding exactly the movement worth seeing.
    Instead it's padded around the observed range, and both endpoints are
    labelled so the scale can't be misread as "they halved their price".
    """
    if not ours and not theirs:
        return Markup('<p class="chart-empty">No price history yet.</p>')

    days = sorted({d for d, _ in ours} | {d for d, _ in theirs})
    if len(days) < 2:
        return Markup(
            '<p class="chart-empty">Only one day of price history so far — '
            "a line needs a second day.</p>"
        )

    values = [v for _, v in ours] + [v for _, v in theirs]
    low, high = min(values), max(values)
    if high - low < 0.01:
        # A perfectly flat pair would divide by zero below; give it a band.
        low, high = low - 1, high + 1
    pad = (high - low) * 0.15
    low, high = low - pad, high + pad

    index = {day: i for i, day in enumerate(days)}
    plot_w = _PRICE_W - _PAD_L - _PAD_R
    plot_h = _PRICE_H - _PAD_T - _PAD_B

    def x_of(day: date) -> float:
        return _PAD_L + plot_w * index[day] / (len(days) - 1)

    def y_of(value: float) -> float:
        return _PAD_T + plot_h * (1 - (value - low) / (high - low))

    def path(series) -> str:
        return " ".join(f"{x_of(d):.1f},{y_of(v):.1f}" for d, v in sorted(series))

    parts = [
        f'<svg class="chart price-chart" viewBox="0 0 {_PRICE_W} {_PRICE_H}" '
        f'role="img" aria-label="Price history, ours against theirs">'
    ]
    for value in (low + pad, high - pad):
        y = y_of(value)
        parts.append(
            f'<line class="grid" x1="{_PAD_L}" y1="{y:.1f}" '
            f'x2="{_PRICE_W - _PAD_R}" y2="{y:.1f}"/>'
            f'<text class="axis" x="{_PAD_L - 6}" y="{y + 4:.1f}" '
            f'text-anchor="end">${value:,.2f}</text>'
        )
    if theirs:
        parts.append(f'<polyline class="theirs" points="{path(theirs)}"/>')
    if ours:
        parts.append(f'<polyline class="ours" points="{path(ours)}"/>')

    parts.append(
        f'<text class="axis" x="{_PAD_L}" y="{_PRICE_H - 6}">{days[0]}</text>'
        f'<text class="axis" x="{_PRICE_W - _PAD_R}" y="{_PRICE_H - 6}" '
        f'text-anchor="end">{days[-1]}</text>'
        f'<text class="legend ours-key" x="{_PAD_L + 90}" y="{_PRICE_H - 6}">'
        f"— {escape(our_label)}</text>"
        f'<text class="legend theirs-key" x="{_PAD_L + 190}" y="{_PRICE_H - 6}">'
        f"— {escape(their_label)}</text>"
        "</svg>"
    )
    return Markup("".join(parts))
