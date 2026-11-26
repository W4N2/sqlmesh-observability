"""Plain-text formatting shared by the report, its payload, and alerts."""

from __future__ import annotations

import json
import typing as t

METRIC_LABELS = {
    "row_count": "Row count",
    "freshness_lag_seconds": "Freshness lag (s)",
    "null_rate": "Null rate",
    "mean": "Mean",
}


def args_text(args_json: t.Optional[str]) -> str:
    """``{"columns": "(id)"}`` -> ``columns := (id)``, the way the model file spells it."""
    if not args_json:
        return ""
    try:
        args = json.loads(args_json)
    except ValueError:
        return args_json
    if not isinstance(args, dict):
        return args_json
    return ", ".join(f"{key} := {value}" for key, value in args.items())


def number(value: t.Optional[float]) -> str:
    if value is None:
        return "-"
    if float(value).is_integer() and abs(value) < 1e15:
        return f"{int(value):,}"
    if abs(value) < 1:
        return f"{value:.3f}"
    return f"{value:,.2f}"


def expected_range(mean: t.Optional[float], stddev: t.Optional[float], k: float) -> str:
    if mean is None or stddev is None:
        return "-"
    lo, hi = mean - k * stddev, mean + k * stddev
    if abs(hi - lo) < 1e-12:
        return number(mean)
    return f"{number(lo)} – {number(hi)}"


def short_name(model_name: str) -> str:
    """``"db"."analytics"."orders"`` -> ``db.analytics.orders`` for display."""
    return model_name.replace('"', "")


def metric_label(metric_name: str, column_name: str) -> str:
    label = METRIC_LABELS.get(metric_name, metric_name)
    return f"{label} ({column_name})" if column_name else label
