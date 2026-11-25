"""Macros used by the shipped anomaly audits.

``sqlmesh-observe init-audits`` copies this file verbatim to
``<project>/macros/observability_audits.py``, so it must stay self-contained: it imports only
SQLMesh and sqlglot.

Why macros: no single SQL spelling of "date plus N days" or "date difference in days" parses the
same way across DuckDB, Snowflake, BigQuery, Postgres, Trino, and others. The audit's interval
bounds and execution time are literals at render time, so the date arithmetic is done here and the
audit SQL only sees ``CAST('YYYY-MM-DD' AS DATE)`` / ``CAST('YYYY-MM-DD HH:MM:SS' AS TIMESTAMP)``
literals, which every engine accepts.
"""

from __future__ import annotations

import datetime
import functools
import typing as t

from sqlglot import exp
from sqlmesh import macro

OBS_MAX_SPINE_DAYS = 10_000


def _obs_text(value: t.Any) -> str:
    return value.name if isinstance(value, exp.Expression) else str(value)


def _obs_date(value: t.Any) -> datetime.date:
    from sqlmesh.utils.date import to_datetime

    return to_datetime(_obs_text(value)).date()


def _obs_int(value: t.Any) -> int:
    return int(float(_obs_text(value)))


def _obs_date_literal(day: datetime.date) -> exp.Expression:
    return exp.cast(exp.Literal.string(day.isoformat()), exp.DataType.build("DATE"))


@macro()
def obs_days_before(evaluator: t.Any, day: t.Any, days: t.Any) -> exp.Expression:
    """``@obs_days_before(@start_ds, 14)`` -> ``CAST('<start_ds - 14 days>' AS DATE)``."""
    return _obs_date_literal(_obs_date(day) - datetime.timedelta(days=_obs_int(days)))


@macro()
def obs_day_spine(
    evaluator: t.Any, start_ds: t.Any, end_ds: t.Any, training_days: t.Any
) -> exp.Query:
    """One row per day, column ``bucket``, from ``start_ds - training_days`` to ``end_ds``."""
    first = _obs_date(start_ds) - datetime.timedelta(days=_obs_int(training_days))
    last = _obs_date(end_ds)
    count = (last - first).days + 1
    if count < 1 or count > OBS_MAX_SPINE_DAYS:
        raise ValueError(
            f"obs_day_spine: {count} days from {first} to {last}; "
            f"expected 1..{OBS_MAX_SPINE_DAYS}. Use a smaller batch_size or training_days."
        )
    selects: t.List[exp.Query] = [
        exp.select(_obs_date_literal(first + datetime.timedelta(days=offset)).as_("bucket"))
        for offset in range(count)
    ]
    return functools.reduce(lambda left, right: exp.union(left, right, distinct=False), selects)


@macro()
def obs_freshness_cutoff(
    evaluator: t.Any, execution_ts: t.Any, end_ts: t.Any, hours: t.Any
) -> exp.Expression:
    """``min(execution_ts, end_ts) - hours`` as a ``CAST('…' AS TIMESTAMP)`` literal."""
    from sqlmesh.utils.date import to_datetime

    reference = min(to_datetime(_obs_text(execution_ts)), to_datetime(_obs_text(end_ts)))
    cutoff = reference - datetime.timedelta(hours=_obs_int(hours))
    text = cutoff.replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")
    return exp.cast(exp.Literal.string(text), exp.DataType.build("TIMESTAMP"))


OBS_COLUMN_METRICS = ("null_rate", "mean")


@macro()
def obs_column_metric(evaluator: t.Any, column: t.Any, metric: t.Any) -> exp.Expr:
    """Per-bucket aggregate for ``column_anomaly``.

    ``null_rate`` -> share of rows where the column is null (0 to 1).
    ``mean`` -> average of the non-null values (``NULL`` when all are null).
    """
    name = _obs_text(metric).lower()
    double = exp.DataType.build("DOUBLE")
    target = column if isinstance(column, exp.Expression) else exp.column(str(column))
    if name == "null_rate":
        nulls = exp.Sub(this=exp.func("COUNT", exp.Star()), expression=exp.func("COUNT", target))
        return exp.Div(this=exp.cast(nulls, double), expression=exp.func("COUNT", exp.Star()))
    if name == "mean":
        return exp.func("AVG", exp.cast(target, double))
    raise ValueError(
        f"column_anomaly: unknown metric {name!r}; use one of {', '.join(OBS_COLUMN_METRICS)}"
    )
