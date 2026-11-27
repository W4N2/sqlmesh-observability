"""Passive volume, freshness, and schema monitors, and per-column metrics.

Runs on the writer thread, after an invocation's base rows are committed: it never runs inside an
observer callback. Metric anomalies are scored at read time (:func:`score_series`), with the same
rule as the ``volume_anomaly`` audit, so changing thresholds needs no backfill.
"""

from __future__ import annotations

import datetime
import logging
import math
import typing as t
from dataclasses import dataclass, field

from sqlglot import exp

if t.TYPE_CHECKING:
    from sqlmesh.core.engine_adapter import EngineAdapter

    from sqlmesh_observability.collector import Invocation, ModelRun, SchemaCapture

logger = logging.getLogger(__name__)

ROW_COUNT = "row_count"
FRESHNESS_LAG = "freshness_lag_seconds"
NULL_RATE = "null_rate"
MEAN = "mean"
COLUMN_METRICS = (NULL_RATE, MEAN)

# Kinds whose "table" is a view or does not exist: counting a view re-runs its query.
_NO_VOLUME_KINDS = frozenset({"VIEW", "EMBEDDED", "EXTERNAL"})
_SCORE_KEYS = (
    "training_days",
    "min_buckets",
    "threshold",
    "spike_failure_percent_threshold",
    "drop_failure_percent_threshold",
)


# --- Settings -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoreSettings:
    """The ``volume_anomaly`` audit's rule, applied to a metric series at read time."""

    training_days: int = 14
    min_buckets: int = 7
    threshold: float = 3.0
    spike_failure_percent_threshold: t.Optional[float] = None
    drop_failure_percent_threshold: t.Optional[float] = None


@dataclass(frozen=True)
class FreshnessSettings:
    column: t.Optional[str] = None
    # (model name as written in config, column) overrides, e.g. (("analytics.orders", "order_ts"),)
    columns: t.Tuple[t.Tuple[str, str], ...] = ()
    score: ScoreSettings = field(default_factory=ScoreSettings)

    def column_for(self, model_name: str) -> t.Optional[str]:
        model_parts = _name_parts(model_name)
        for name, column in self.columns:
            key = _name_parts(name)
            if key and model_parts[-len(key) :] == key:
                return column
        return self.column


@dataclass(frozen=True)
class MonitorSettings:
    volume: t.Optional[ScoreSettings] = None
    freshness: t.Optional[FreshnessSettings] = None
    schema: bool = False
    # (model key as written in config, ((metric, column), ...)).
    columns: t.Tuple[t.Tuple[str, t.Tuple[t.Tuple[str, str], ...]], ...] = ()

    @property
    def collects_metrics(self) -> bool:
        return self.volume is not None or self.freshness is not None or bool(self.columns)

    @property
    def enabled(self) -> bool:
        return self.collects_metrics or self.schema

    def columns_for(self, model_name: str) -> t.Tuple[t.Tuple[str, str], ...]:
        """(metric, column) pairs configured for ``model_name``, from every matching key."""
        model_parts = _name_parts(model_name)
        found: t.List[t.Tuple[str, str]] = []
        for key, pairs in self.columns:
            parts = _name_parts(key)
            if parts and model_parts[-len(parts) :] == parts:
                found.extend(pair for pair in pairs if pair not in found)
        return tuple(found)

    def score_for(self, metric_name: str) -> ScoreSettings:
        if metric_name == ROW_COUNT and self.volume is not None:
            return self.volume
        if metric_name == FRESHNESS_LAG and self.freshness is not None:
            return self.freshness.score
        return ScoreSettings()


def _name_parts(name: str) -> t.Tuple[str, ...]:
    try:
        return tuple(part.name.lower() for part in exp.to_table(name).parts)
    except Exception:
        return tuple(part.strip('"`').lower() for part in name.split("."))


def _score_settings(options: t.Mapping[str, t.Any], monitor: str) -> ScoreSettings:
    values: t.Dict[str, t.Any] = {}
    for key in _SCORE_KEYS:
        if options.get(key) is not None:
            values[key] = options[key]
    try:
        return ScoreSettings(**values)
    except TypeError:
        logger.warning("sqlmesh-observability: bad scoring settings for %s monitor", monitor)
        return ScoreSettings()


def normalize_monitors(monitors: t.Optional[t.Mapping[str, t.Any]]) -> MonitorSettings:
    """Turn ``install(monitors=...)`` into settings. Unknown or malformed parts are ignored."""
    if not monitors:
        return MonitorSettings()
    if not isinstance(monitors, t.Mapping):
        logger.warning("sqlmesh-observability: `monitors` must be a dict; passive monitors off")
        return MonitorSettings()
    unknown = set(monitors) - {"volume", "freshness", "schema", "columns"}
    if unknown:
        logger.warning("sqlmesh-observability: unknown monitors ignored: %s", sorted(unknown))

    volume: t.Optional[ScoreSettings] = None
    raw_volume = monitors.get("volume")
    if raw_volume is True:
        volume = ScoreSettings()
    elif isinstance(raw_volume, t.Mapping):
        volume = _score_settings(raw_volume, "volume")

    freshness: t.Optional[FreshnessSettings] = None
    raw_freshness = monitors.get("freshness")
    if isinstance(raw_freshness, t.Mapping):
        overrides = raw_freshness.get("columns") or {}
        column = raw_freshness.get("column")
        if column or overrides:
            freshness = FreshnessSettings(
                column=str(column) if column else None,
                columns=tuple((str(k), str(v)) for k, v in dict(overrides).items()),
                score=_score_settings(raw_freshness, "freshness"),
            )
    elif raw_freshness:
        logger.warning(
            "sqlmesh-observability: `freshness` needs {'column': ...}; freshness monitor off"
        )

    return MonitorSettings(
        volume=volume,
        freshness=freshness,
        schema=bool(monitors.get("schema")),
        columns=_column_settings(monitors.get("columns")),
    )


def _column_settings(
    raw: t.Any,
) -> t.Tuple[t.Tuple[str, t.Tuple[t.Tuple[str, str], ...]], ...]:
    """``{"orders": {"null_rate": ["status"], "mean": ["amount"]}}`` -> tuples, in config order."""
    if not raw:
        return ()
    if not isinstance(raw, t.Mapping):
        logger.warning("sqlmesh-observability: `columns` must be a dict; column monitors off")
        return ()
    settings: t.List[t.Tuple[str, t.Tuple[t.Tuple[str, str], ...]]] = []
    for model_key, metrics in raw.items():
        if not isinstance(metrics, t.Mapping):
            logger.warning("sqlmesh-observability: columns[%r] must be a dict; ignored", model_key)
            continue
        pairs: t.List[t.Tuple[str, str]] = []
        for metric, columns in metrics.items():
            if metric not in COLUMN_METRICS:
                logger.warning(
                    "sqlmesh-observability: unknown column metric %r ignored (use %s)",
                    metric,
                    ", ".join(COLUMN_METRICS),
                )
                continue
            names = [columns] if isinstance(columns, str) else list(columns or ())
            pairs.extend((str(metric), str(column)) for column in names)
        if pairs:
            settings.append((str(model_key), tuple(pairs)))
    return tuple(settings)


# --- Metric collection (writer thread) ------------------------------------------------------------


def _ms_to_datetime(value: t.Optional[int]) -> t.Optional[datetime.datetime]:
    if value is None:
        return None
    return datetime.datetime.fromtimestamp(value / 1000, tz=datetime.timezone.utc)


def _first(row: t.Optional[t.Tuple[t.Any, ...]]) -> t.Any:
    """The first value of a fetched row, or None when the query returned no row."""
    return row[0] if row else None


def _naive_utc(value: t.Any) -> t.Optional[datetime.datetime]:
    """A warehouse timestamp as naive UTC (SQLMesh treats naive timestamps as UTC)."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day)
    from sqlmesh.utils.date import to_datetime

    return to_datetime(value).astimezone(datetime.timezone.utc).replace(tzinfo=None)


def _find_column(columns: t.Sequence[t.Tuple[str, str]], wanted: str) -> t.Optional[str]:
    for name, _ in columns:
        if name.lower() == wanted.lower():
            return name
    return None


def _interval_filter(run: "ModelRun", adapter: "EngineAdapter") -> t.Optional[exp.Expression]:
    """SQLMesh's own interval predicate (``Model.render_query``), rebuilt from plain values.

    The time column's type comes from the physical table, as SQLMesh's audit rendering does.
    """
    from sqlmesh.utils.date import make_inclusive, to_time_column

    assert run.time_column is not None and run.physical_table is not None
    if run.interval_start is None or run.interval_end is None:
        return None
    types = adapter.columns(run.physical_table)
    # The table's own spelling, which a quoted query must use on case-sensitive engines.
    found = next(((k, v) for k, v in types.items() if k.lower() == run.time_column.lower()), None)
    if found is None:
        return None
    time_name, time_type = found
    dialect = run.model_dialect or ""  # SQLMesh's default dialect
    low, high = [
        to_time_column(value, time_type, dialect, run.time_column_format)
        for value in make_inclusive(run.interval_start, run.interval_end, run.model_dialect)
    ]
    return exp.column(time_name).between(low, high)


def _candidates(invocation: "Invocation") -> t.List["ModelRun"]:
    """Successful, non-audit-only runs, keeping the last batch per (model, version, interval)."""
    latest: t.Dict[t.Tuple[str, str, t.Optional[int]], "ModelRun"] = {}
    for run in invocation.models:
        if run.status != "success" or run.audit_only or not run.physical_table:
            continue
        latest[(run.model_name, run.version, run.interval_start)] = run
    return list(latest.values())


def collect_metrics(
    invocation: "Invocation",
    data_adapter: "EngineAdapter",
    log_adapter: "EngineAdapter",
    settings: MonitorSettings,
) -> t.List[t.Dict[str, t.Any]]:
    """Run the read-only metric queries for ``invocation`` and return ``metrics`` rows.

    ``log_adapter`` is unused here; it is passed so callers (and tests) see one signature.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    rows: t.List[t.Dict[str, t.Any]] = []
    for run in _candidates(invocation):
        if (run.kind or "") in _NO_VOLUME_KINDS:
            continue
        measure = _Measure(run, data_adapter, now)
        if settings.volume is not None:
            rows.extend(measure.volume())
        if settings.freshness is not None:
            rows.extend(measure.freshness(settings.freshness))
        pairs = settings.columns_for(run.model_name)
        if pairs:
            rows.extend(measure.columns(pairs))
    return rows


class _Measure:
    """Metric queries for one model interval run."""

    def __init__(self, run: "ModelRun", adapter: "EngineAdapter", now: datetime.datetime) -> None:
        self.run = run
        self.adapter = adapter
        self.table = exp.to_table(str(run.physical_table), dialect=adapter.dialect)
        self.base = {
            "invocation_id": run.invocation_id,
            "model_name": run.model_name,
            "version": run.version,
            "physical_table": run.physical_table,
            "interval_start": _ms_to_datetime(run.interval_start),
            "interval_end": _ms_to_datetime(run.interval_end),
            "column_name": "",
            "updated_at": now,
        }

    def _row(self, metric: str, column: str = "", **values: t.Any) -> t.Dict[str, t.Any]:
        return {**self.base, "metric_name": metric, "column_name": column, **values}

    def _fetchone(self, query: exp.Expr) -> t.Optional[t.Tuple[t.Any, ...]]:
        # Quoted, as SQLMesh's own audits are: every name here is the warehouse's real one (the
        # physical table SQLMesh created, columns as declared or described), and a lower-case
        # one such as SQLMesh's "sqlmesh__<schema>" must not be upper-cased by Snowflake.
        return self.adapter.fetchone(query, quote_identifiers=True)

    def _interval_query(self, *projections: exp.Expr) -> exp.Select:
        """``SELECT ... FROM table`` over the evaluated interval, or the whole table."""
        query = exp.select(*projections).from_(self.table)
        if self.run.time_column:
            where = _interval_filter(self.run, self.adapter)
            if where is None:
                raise ValueError("no interval filter for a time-partitioned model")
            query = query.where(where)
        return query

    def volume(self) -> t.List[t.Dict[str, t.Any]]:
        try:
            count = _first(self._fetchone(self._interval_query(exp.func("COUNT", exp.Star()))))
            return [self._row(ROW_COUNT, metric_value=float(count or 0))]
        except Exception as error:
            logger.debug("sqlmesh-observability: row count failed", exc_info=True)
            return [self._row(ROW_COUNT, error_message=str(error))]

    def freshness(self, settings: FreshnessSettings) -> t.List[t.Dict[str, t.Any]]:
        wanted = settings.column_for(self.run.model_name)
        column = _find_column(self.run.columns, wanted) if wanted else None
        if column is None:
            return []  # the model has no such column: skip without error
        try:
            query = exp.select(exp.func("MAX", exp.column(column))).from_(self.table)
            latest = _naive_utc(_first(self._fetchone(query)))
            executed = _naive_utc(_ms_to_datetime(self.run.execution_time))
            value = (
                None if latest is None or executed is None else (executed - latest).total_seconds()
            )
            return [self._row(FRESHNESS_LAG, metric_value=value)]
        except Exception as error:
            logger.debug("sqlmesh-observability: freshness query failed", exc_info=True)
            return [self._row(FRESHNESS_LAG, error_message=str(error))]

    def _available_columns(self) -> t.Sequence[t.Tuple[str, str]]:
        if self.run.columns:
            return self.run.columns
        return [(str(name), "") for name in self.adapter.columns(str(self.run.physical_table))]

    def columns(self, pairs: t.Sequence[t.Tuple[str, str]]) -> t.List[t.Dict[str, t.Any]]:
        """``null_rate`` / ``mean`` for configured columns, in one query."""
        rows: t.List[t.Dict[str, t.Any]] = []
        try:
            available = self._available_columns()
        except Exception as error:
            return [self._row(m, c, error_message=str(error)) for m, c in pairs]

        measured: t.List[t.Tuple[str, str, str]] = []  # (metric, configured name, real name)
        for metric, wanted in pairs:
            real = _find_column(available, wanted)
            if real is None:
                rows.append(self._row(metric, wanted, error_message=f"column {wanted!r} not found"))
            else:
                measured.append((metric, wanted, real))
        if not measured:
            return rows

        double = exp.DataType.build("DOUBLE")
        projections: t.List[exp.Expr] = [exp.func("COUNT", exp.Star())]
        for metric, _, real in measured:
            if metric == NULL_RATE:
                projections.append(exp.func("COUNT", exp.column(real)))
            else:
                projections.append(exp.func("AVG", exp.cast(exp.column(real), double)))
        try:
            result = self._fetchone(self._interval_query(*projections))
        except Exception as error:
            logger.debug("sqlmesh-observability: column metrics failed", exc_info=True)
            return rows + [self._row(m, c, error_message=str(error)) for m, c, _ in measured]
        if result is None:
            return rows + [self._row(m, c, error_message="no result") for m, c, _ in measured]

        total = int(result[0] or 0)
        for (metric, wanted, _), value in zip(measured, result[1:]):
            if metric == NULL_RATE:
                metric_value = None if total == 0 else (total - int(value or 0)) / total
            else:
                metric_value = None if value is None else float(value)
            rows.append(self._row(metric, wanted, metric_value=metric_value))
        return rows


# --- Schema diffing (writer thread) ------------------------------------------------------------


@dataclass
class RecordedSchema:
    snapshot_id: str
    version: str
    recorded_at: datetime.datetime
    columns: t.Tuple[t.Tuple[str, str], ...]


def _prior(
    capture: "SchemaCapture", recorded: t.Mapping[str, RecordedSchema]
) -> t.Optional[RecordedSchema]:
    """The capture this one descends from: its newest recorded ancestor."""
    for snapshot_id in reversed(capture.previous_snapshot_ids):
        if snapshot_id != capture.snapshot_id and snapshot_id in recorded:
            return recorded[snapshot_id]
    if capture.previous_snapshot_ids:
        return None  # an ancestry exists but none of it was recorded: nothing to compare with
    others = [r for r in recorded.values() if r.snapshot_id != capture.snapshot_id]
    return max(others, key=lambda r: r.recorded_at) if others else None


def schema_rows(
    invocation: "Invocation", recorded: t.Dict[str, t.Dict[str, RecordedSchema]]
) -> t.Tuple[t.List[t.Dict[str, t.Any]], t.List[t.Dict[str, t.Any]]]:
    """``schema_columns`` and ``schema_changes`` rows for the invocation's captures.

    ``recorded`` maps model name to the captures already in ``schema_columns`` by snapshot id; it
    is updated in place, so two captures of one model in one invocation chain correctly.
    """
    column_rows: t.List[t.Dict[str, t.Any]] = []
    change_rows: t.List[t.Dict[str, t.Any]] = []
    for capture in invocation.schemas:
        by_snapshot = recorded.setdefault(capture.model_name, {})
        prior = _prior(capture, by_snapshot)
        for ordinal, (name, kind) in enumerate(capture.columns):
            column_rows.append(
                {
                    "invocation_id": capture.invocation_id,
                    "model_name": capture.model_name,
                    "snapshot_id": capture.snapshot_id,
                    "version": capture.version,
                    "ordinal": ordinal,
                    "column_name": name,
                    "data_type": kind,
                    "recorded_at": capture.recorded_at,
                }
            )
        if prior is not None:
            for name, change, old_type, new_type in diff_columns(prior.columns, capture.columns):
                change_rows.append(
                    {
                        "invocation_id": capture.invocation_id,
                        "model_name": capture.model_name,
                        "from_snapshot_id": prior.snapshot_id,
                        "to_snapshot_id": capture.snapshot_id,
                        "from_version": prior.version,
                        "to_version": capture.version,
                        "column_name": name,
                        "change_type": change,
                        "old_type": old_type,
                        "new_type": new_type,
                        "recorded_at": capture.recorded_at,
                    }
                )
        by_snapshot[capture.snapshot_id] = RecordedSchema(
            snapshot_id=capture.snapshot_id,
            version=capture.version,
            recorded_at=capture.recorded_at,
            columns=capture.columns,
        )
    return column_rows, change_rows


def diff_columns(
    old: t.Sequence[t.Tuple[str, str]], new: t.Sequence[t.Tuple[str, str]]
) -> t.List[t.Tuple[str, str, t.Optional[str], t.Optional[str]]]:
    """(column, change_type, old_type, new_type) for added, removed, and retyped columns."""
    old_map, new_map = dict(old), dict(new)
    changes: t.List[t.Tuple[str, str, t.Optional[str], t.Optional[str]]] = []
    for name, kind in new:
        if name not in old_map:
            changes.append((name, "added", None, kind))
        elif old_map[name] != kind:
            changes.append((name, "retyped", old_map[name], kind))
    for name, kind in old:
        if name not in new_map:
            changes.append((name, "removed", kind, None))
    return changes


# --- Read-time scoring ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoredPoint:
    interval_start: datetime.datetime
    value: t.Optional[float]
    mean: t.Optional[float]
    stddev: t.Optional[float]
    training_points: int
    anomaly: bool


def _flagged(value: float, mean: float, stddev: float, settings: ScoreSettings) -> bool:
    if abs(value - mean) <= settings.threshold * stddev:
        return False
    if value >= mean:
        spike = settings.spike_failure_percent_threshold
        return spike is None or value > (1 + spike / 100.0) * mean
    drop = settings.drop_failure_percent_threshold
    return drop is None or value < (1 - drop / 100.0) * mean


def score_series(
    points: t.Sequence[t.Tuple[datetime.datetime, t.Optional[float]]],
    settings: ScoreSettings,
) -> t.List[ScoredPoint]:
    """Score each point against the earlier points within ``training_days`` before it.

    ``points`` must be sorted by time with one value per interval. Same rule as ``volume_anomaly``:
    sample standard deviation, ``|value - mean| > threshold * stddev``, and the optional
    ignore-small-changes percent thresholds, each only in its own direction.
    """
    scored: t.List[ScoredPoint] = []
    window = datetime.timedelta(days=settings.training_days)
    for index, (when, value) in enumerate(points):
        training = [v for ts, v in points[:index] if v is not None and when - window <= ts < when]
        n = len(training)
        mean = stddev = None
        anomaly = False
        if n >= max(settings.min_buckets, 2):
            mean = sum(training) / n
            stddev = math.sqrt(sum((x - mean) ** 2 for x in training) / (n - 1))
            if value is not None:
                anomaly = _flagged(value, mean, stddev, settings)
        scored.append(
            ScoredPoint(
                interval_start=when,
                value=value,
                mean=mean,
                stddev=stddev,
                training_points=n,
                anomaly=anomaly,
            )
        )
    return scored
