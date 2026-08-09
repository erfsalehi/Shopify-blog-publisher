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
