"""Inline SVG charts for the report.

Geometry is computed here and written as plain ``<svg>``, so the report needs no chart library and
works offline. Colors come from CSS custom properties defined in ``report.py`` (``--series``,
``--band``, status colors), so light and dark themes apply without touching the markup.

Accessibility: every mark carries a ``<title>``; anomalies use the warning (amber) state colour
*and* a larger ringed marker *and* a text note beside the chart, so colour is never the only cue;
each health cell carries its status letter. Red stays reserved for blocking failures and errors.
"""

from __future__ import annotations

import html
import typing as t

if t.TYPE_CHECKING:
    from sqlmesh_observability.reader import IntervalHealth, MetricSeries, RunRecord

# Counts and lags are magnitudes read against zero; rates and means are read against their band.
ZERO_BASELINE_METRICS = frozenset({"row_count", "freshness_lag_seconds"})
MIN_BAND_PX = 8.0  # reads as a halo around a 2px line on a flat history

WIDTH = 920
CHART_HEIGHT = 170
PAD_LEFT = 56
PAD_RIGHT = 12
PAD_TOP = 12
PAD_BOTTOM = 26

STATUS_LETTER = {"pass": "P", "warn": "W", "fail": "F", "error": "E", "skip": "S"}
STATUS_LABEL = {
    "pass": "pass",
    "warn": "non-blocking fail",
    "fail": "blocking fail",
    "error": "error",
    "skip": "skipped",
}


def _esc(value: t.Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def fmt(value: t.Optional[float]) -> str:
    if value is None:
        return "-"
    if float(value).is_integer() and abs(value) < 1e15:
        return f"{int(value):,}"
    if abs(value) < 1:
        return f"{value:.3f}"
    return f"{value:,.2f}"


def _f(value: float) -> str:
    return f"{value:.1f}"


class _Scale:
    def __init__(self, lo: float, hi: float, count: int, height: int) -> None:
        if hi <= lo:
            hi = lo + (abs(lo) or 1.0)
        self.lo, self.hi, self.count, self.height = lo, hi, count, height
        self.plot_w = WIDTH - PAD_LEFT - PAD_RIGHT
        self.plot_h = height - PAD_TOP - PAD_BOTTOM

    def x(self, index: int) -> float:
        if self.count <= 1:
            return PAD_LEFT + self.plot_w / 2
        return PAD_LEFT + self.plot_w * index / (self.count - 1)

    def y(self, value: float) -> float:
        value = min(max(value, self.lo), self.hi)
        return PAD_TOP + self.plot_h * (1 - (value - self.lo) / (self.hi - self.lo))


def _bounds(series: "MetricSeries") -> t.List[t.Optional[t.Tuple[float, float]]]:
    k = series.threshold
    return [
        None
        if p.mean is None or p.stddev is None
        else (p.mean - k * p.stddev, p.mean + k * p.stddev)
        for p in series.points
    ]


def metric_chart(series: "MetricSeries", label: str) -> str:
    """Values as a line and points over the expected band, with anomaly markers."""
    points = series.points
    bounds = _bounds(series)
    values = [p.value for p in points if p.value is not None]
    edges = [edge for b in bounds if b is not None for edge in b]
    if not values:
        return '<p class="empty">No values to chart yet.</p>'

    lo = min(values + edges)
    hi = max(values + edges)
    if series.metric_name in ZERO_BASELINE_METRICS and min(values) >= 0:
        lo = 0.0
        span = (hi - lo) or abs(hi) or 1.0
        scale = _Scale(lo, hi + span * 0.05, len(points), CHART_HEIGHT)
    else:
        span = (hi - lo) or abs(hi) or 1.0
        lo -= span * 0.1
        scale = _Scale(lo, hi + span * 0.1, len(points), CHART_HEIGHT)

    parts: t.List[str] = []
    # Recessive frame: baseline, top gridline, and min/max value labels.
    base_y, top_y = scale.y(scale.lo), scale.y(hi)  # label the data maximum, not the padding
    parts.append(
        f'<line class="grid" x1="{PAD_LEFT}" x2="{WIDTH - PAD_RIGHT}" '
        f'y1="{_f(top_y)}" y2="{_f(top_y)}"></line>'
        f'<line class="axis" x1="{PAD_LEFT}" x2="{WIDTH - PAD_RIGHT}" '
        f'y1="{_f(base_y)}" y2="{_f(base_y)}"></line>'
        f'<text class="tick" x="{PAD_LEFT - 6}" y="{_f(top_y + 4)}" text-anchor="end">'
        f"{_esc(fmt(hi))}</text>"
        f'<text class="tick" x="{PAD_LEFT - 6}" y="{_f(base_y + 4)}" text-anchor="end">'
        f"{_esc(fmt(scale.lo))}</text>"
    )

    # Expected band, one closed path per run of points that have a training window.
    runs: t.List[t.List[int]] = []
    for index, bound in enumerate(bounds):
        if bound is None:
            if runs and runs[-1]:
                runs.append([])
            continue
        if not runs:
            runs.append([])
        runs[-1].append(index)
    for indexes in (r for r in runs if r):
        upper = [
            (scale.x(i), scale.y(t.cast(t.Tuple[float, float], bounds[i])[1])) for i in indexes
        ]
        lower = [
            (scale.x(i), scale.y(t.cast(t.Tuple[float, float], bounds[i])[0])) for i in indexes
        ]
        # A near-flat history still gets a band you can see.
        for j, ((ux, uy), (lx, ly)) in enumerate(zip(upper, lower)):
            if ly - uy < MIN_BAND_PX:
                mid = (uy + ly) / 2
                upper[j], lower[j] = (ux, mid - MIN_BAND_PX / 2), (lx, mid + MIN_BAND_PX / 2)
        if len(indexes) == 1:  # a single point still gets a visible sliver
            x, _ = upper[0]
            upper = [(x - 3, upper[0][1]), (x + 3, upper[0][1])]
            lower = [(x - 3, lower[0][1]), (x + 3, lower[0][1])]
        outline = upper + list(reversed(lower))
        d = "M" + " L".join(f"{_f(x)},{_f(y)}" for x, y in outline) + " Z"
        parts.append(f'<path class="band" d="{d}"><title>Expected range</title></path>')

    # Values line, broken where a value is missing.
    d_parts: t.List[str] = []
    pen_down = False
    for index, point in enumerate(points):
        if point.value is None:
            pen_down = False
            continue
        command = "L" if pen_down else "M"
        d_parts.append(f"{command}{_f(scale.x(index))},{_f(scale.y(point.value))}")
        pen_down = True
    parts.append(f'<path class="line" d="{" ".join(d_parts)}"></path>')

    # Points, anomaly rings, and generous hit targets with the same titles.
    for index, (point, bound) in enumerate(zip(points, bounds)):
        if point.value is None:
            continue
        x, y = scale.x(index), scale.y(point.value)
        expected = (
            "no training window yet"
            if bound is None
            else f"expected {fmt(bound[0])} to {fmt(bound[1])}"
        )
        title = f"{point.interval_start}: {fmt(point.value)} ({expected})"
        marker = (
            f'<circle class="point" cx="{_f(x)}" cy="{_f(y)}" r="4">'
            f"<title>{_esc(title)}</title></circle>"
        )
        if point.anomaly:
            title = f"Anomaly at {title}"
            marker += (
                f'<circle class="anomaly" cx="{_f(x)}" cy="{_f(y)}" r="6">'
                f"<title>{_esc(title)}</title></circle>"
            )
        parts.append(
            marker + f'<circle class="hit" cx="{_f(x)}" cy="{_f(y)}" r="12">'
            f"<title>{_esc(title)}</title></circle>"
        )

    # First and last interval under the axis.
    first, last = points[0].interval_start[:10], points[-1].interval_start[:10]
    parts.append(
        f'<text class="tick" x="{PAD_LEFT}" y="{CHART_HEIGHT - 8}">{_esc(first)}</text>'
        f'<text class="tick" x="{WIDTH - PAD_RIGHT}" y="{CHART_HEIGHT - 8}" text-anchor="end">'
        f"{_esc(last)}</text>"
    )

    anomalies = sum(1 for p in points if p.anomaly)
    summary = (
        f"{label} ({series.metric_name}), {len(points)} intervals, "
        f"{anomalies} anomal{'y' if anomalies == 1 else 'ies'}"
    )
    return (
        f'<svg class="chart" viewBox="0 0 {WIDTH} {CHART_HEIGHT}" role="img" '
        f'aria-label="{_esc(summary)}" preserveAspectRatio="xMidYMid meet">'
        f"<title>{_esc(summary)}</title>{''.join(parts)}</svg>"
    )


def health_strip(health: t.Sequence["IntervalHealth"], compact: bool = False) -> str:
    """One labelled cell per interval, oldest first.

    ``compact`` drops the row label and letters for the dashboard's one-row-per-model table.
    """
    if not health:
        return ""
    count = len(health)
    gap = 2
    left = 0 if compact else PAD_LEFT
    max_cell = 12.0 if compact else 22.0
    cell = max(3.0, min(max_cell, (WIDTH - left - PAD_RIGHT - gap * (count - 1)) / count))
    height = 16 if compact else 22
    parts: t.List[str] = []
    if not compact:
        parts.append(
            f'<text class="tick" x="{PAD_LEFT - 6}" y="{height / 2 + 4}" text-anchor="end">'
            "Intervals</text>"
        )
    for index, item in enumerate(health):
        x = left + index * (cell + gap)
        label = STATUS_LABEL.get(item.status, item.status)
        title = f"{item.interval_start}: {item.status} ({label}). {item.detail}"
        parts.append(
            f'<rect class="cell s-{_esc(item.status)}" data-status="{_esc(item.status)}" '
            f'x="{_f(x)}" y="0" width="{_f(cell)}" height="{height}" rx="2">'
            f"<title>{_esc(title)}</title></rect>"
        )
        if not compact and cell >= 12:
            parts.append(
                f'<text class="cell-letter s-{_esc(item.status)}" x="{_f(x + cell / 2)}" '
                f'y="{height / 2 + 4}" text-anchor="middle" aria-hidden="true">'
                f"{STATUS_LETTER.get(item.status, '?')}</text>"
            )
    width = left + count * (cell + gap) - gap
    counts: t.Dict[str, int] = {}
    for item in health:
        counts[item.status] = counts.get(item.status, 0) + 1
    summary = "Interval health: " + ", ".join(
        f"{n} {STATUS_LABEL.get(status, status)}" for status, n in sorted(counts.items())
    )
    natural = max(width, left + 40)
    return (
        f'<svg class="strip{" compact" if compact else ""}" viewBox="0 0 {_f(natural)} {height}" '
        f'width="{_f(natural)}" height="{height}" role="img" aria-label="{_esc(summary)}">'
        f"<title>{_esc(summary)}</title>{''.join(parts)}</svg>"
    )


def duration_sparkline(runs: t.Sequence["RunRecord"]) -> str:
    """``duration_ms`` per run, in run order, with min and max labels."""
    timed = [r for r in runs if r.duration_ms is not None and not r.audit_only]
    if len(timed) < 2:
        return ""
    height = 48
    values = [float(t.cast(int, r.duration_ms)) for r in timed]
    lo, hi = 0.0, max(values)
    scale = _Scale(lo, hi * 1.1 or 1.0, len(values), height + PAD_TOP + 4)
    coords = [(scale.x(i), scale.y(v)) for i, v in enumerate(values)]
    d = "M" + " L".join(f"{_f(x)},{_f(y)}" for x, y in coords)
    dots = "".join(
        f'<circle class="spark-point" cx="{_f(x)}" cy="{_f(y)}" r="2.5">'
        f"<title>{_esc(run.interval_start or '-')} (run started {_esc(run.started_at or '-')}): "
        f"{fmt(run.duration_ms)} ms</title></circle>"
        for (x, y), run in zip(coords, timed)
    )
    summary = (
        f"Run duration: {len(values)} runs, min {fmt(min(values))} ms, max {fmt(max(values))} ms, "
        f"latest {fmt(values[-1])} ms"
    )
    top_y = scale.y(max(values))
    return (
        f'<svg class="spark" viewBox="0 0 {WIDTH} {scale.height}" role="img" '
        f'aria-label="{_esc(summary)}"><title>{_esc(summary)}</title>'
        f'<text class="tick" x="{PAD_LEFT - 6}" y="{_f(top_y + 4)}" text-anchor="end">'
        f"{_esc(fmt(max(values)))} ms</text>"
        f'<path class="duration" d="{d}"></path>{dots}</svg>'
    )
