"""Read-only warehouse and SQLMesh state loader for reports and alerts.

Loads project gateway settings through ``sqlmesh.core.config.loader.load_configs`` without
constructing a ``Context``, loading models, or creating a scheduler. SQLMesh state is accessed only
through ``EngineAdapterStateSync``'s read methods (never raw SQL, never writes).
"""

from __future__ import annotations

import datetime
import logging
import typing as t
from dataclasses import dataclass
from pathlib import Path

from sqlglot import exp

if t.TYPE_CHECKING:
    from sqlmesh.core.config import Config
    from sqlmesh.core.engine_adapter import EngineAdapter

logger = logging.getLogger(__name__)

DEFAULT_SCHEMA = "observability"
DEFAULT_ENVIRONMENT = "prod"
_IN_CHUNK = 500


class MissingAuditTableError(RuntimeError):
    """Raised when the configured observability schema has no ``audit_results`` table."""

    def __init__(self, schema: str) -> None:
        self.schema = schema
        super().__init__(f"No audit_results table found in schema '{schema}'.")


@dataclass(frozen=True)
class AuditRecord:
    """One normalized audit result row, with any sampled failing rows attached."""

    invocation_id: str
    model_name: str
    snapshot_id: t.Optional[str]
    version: t.Optional[str]
    physical_table: t.Optional[str]
    environment: t.Optional[str]
    audit_name: str
    args_json: t.Optional[str]
    interval_start: t.Optional[str]
    interval_end: t.Optional[str]
    status: str
    violation_count: t.Optional[int]
    blocking: t.Optional[bool]
    query_sql: t.Optional[str]
    error_message: t.Optional[str]
    started_at: t.Optional[str]
    samples: t.Tuple[str, ...] = ()


@dataclass(frozen=True)
class MetricPoint:
    """One interval of a passive metric series, scored at read time."""

    interval_start: str
    value: t.Optional[float]
    mean: t.Optional[float]
    stddev: t.Optional[float]
    training_points: int
    anomaly: bool
    error_message: t.Optional[str] = None
    invocation_id: t.Optional[str] = None  # the invocation that wrote this value (alerts)


@dataclass(frozen=True)
class MetricSeries:
    metric_name: str
    column_name: str
    points: t.Tuple[MetricPoint, ...]
    # Scoring threshold the points were flagged with: the expected band is mean ± threshold·sd.
    threshold: float = 3.0


@dataclass(frozen=True)
class RunRecord:
    """One scoped ``model_runs`` row."""

    invocation_id: str
    snapshot_id: t.Optional[str]
    version: t.Optional[str]
    interval_start: t.Optional[str]
    interval_end: t.Optional[str]
    status: t.Optional[str]
    duration_ms: t.Optional[int]
    started_at: t.Optional[str]  # the invocation's start
    environment: t.Optional[str]
    audit_only: bool
    error_message: t.Optional[str]


@dataclass(frozen=True)
class InvocationRecord:
    """One plan or run that targeted the environment or produced rows in scope (run log)."""

    invocation_id: str
    command: t.Optional[str]
    environment: t.Optional[str]
    status: t.Optional[str]
    started_at: t.Optional[str]
    finished_at: t.Optional[str]
    duration_ms: t.Optional[int]
    model_runs: int = 0
    failed_runs: int = 0
    audits: int = 0
    failing_audits: int = 0
    blocking_failures: int = 0  # failing blocking audits plus audit errors


@dataclass(frozen=True)
class IntervalHealth:
    """Worst outcome of the latest run of one interval."""

    interval_start: str
    status: str  # pass, warn, fail, error, skip
    detail: str


@dataclass(frozen=True)
class MetricAnomaly:
    """A series whose latest point is anomalous."""

    model_name: str
    metric_name: str
    column_name: str
    point: MetricPoint


@dataclass(frozen=True)
class SchemaChange:
    column_name: str
    change_type: str
    old_type: t.Optional[str]
    new_type: t.Optional[str]
    from_version: t.Optional[str]
    to_version: t.Optional[str]
    from_snapshot_id: t.Optional[str]
    to_snapshot_id: t.Optional[str]
    recorded_at: t.Optional[str]


@dataclass(frozen=True)
class ModelReport:
    """Scoped audit history and state metadata for one model."""

    model_name: str
    owner: t.Optional[str]
    columns: t.Tuple[t.Tuple[str, str], ...]
    audits: t.Tuple[AuditRecord, ...]
    failing_blocking_audits: t.Tuple[AuditRecord, ...]
    warning_audits: t.Tuple[AuditRecord, ...] = ()
    metrics: t.Tuple[MetricSeries, ...] = ()
    schema_changes: t.Tuple[SchemaChange, ...] = ()
    runs: t.Tuple[RunRecord, ...] = ()  # oldest first
    health: t.Tuple[IntervalHealth, ...] = ()  # oldest interval first


@dataclass(frozen=True)
class ReportData:
    """Plain data model passed to the static HTML renderer (and later ASGI/alert consumers)."""

    environment: str
    schema: str
    gateway: str
    generated_at: str
    failing_model_count: int
    passing_audit_count: int
    failing_models: t.Tuple[ModelReport, ...]
    models: t.Tuple[ModelReport, ...]
    incidents: t.Tuple[AuditRecord, ...]
    warning_model_count: int = 0
    warning_models: t.Tuple[ModelReport, ...] = ()
    # False when SQLMesh state for the environment could not be read (or does not exist yet). The
    # report then shows only the latest invocation and says so.
    state_available: bool = True
    state_gateway: t.Optional[str] = None
    anomalies: t.Tuple[MetricAnomaly, ...] = ()
    invocations: t.Tuple["InvocationRecord", ...] = ()  # newest first
    # Passive monitors configured in install(): any of volume, freshness, schema, columns.
    monitors: t.Tuple[str, ...] = ()


@dataclass(frozen=True)
class _StateSnapshotInfo:
    active_versions: t.FrozenSet[t.Tuple[str, str]]
    owners: t.Dict[str, t.Optional[str]]
    columns: t.Dict[str, t.Tuple[t.Tuple[str, str], ...]]
    gateway: t.Optional[str] = None


def _fmt_ts(value: t.Any) -> t.Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, datetime.date):
        return value.isoformat()
    return str(value)


def _table(schema: str, name: str) -> exp.Table:
    return exp.to_table(f"{schema}.{name}")


def _load_project_config(path: t.Union[str, Path]) -> t.Tuple["Config", t.Dict[str, t.Any]]:
    """Load SQLMesh ``Config`` without creating a ``Context`` or mutating global collector state.

    Returns ``(config, install_settings)`` where ``install_settings`` contains any values captured
    if ``config.py`` called ``sqlmesh_observability.install()`` during load.
    """
    from sqlmesh.core.config import Config
    from sqlmesh.core.config.loader import load_configs

    from sqlmesh_observability.collector import get_collector

    collector = get_collector()
    previous_settings = collector.settings
    previous_writer = collector.writer
    previous_dialect = collector.dialect
    collector.settings = {}
    try:
        configs = load_configs(None, Config, [Path(path)])
        loaded_settings = dict(collector.settings)
    finally:
        collector.settings = previous_settings
        collector.writer = previous_writer
        collector.dialect = previous_dialect

    if not configs:
        raise ValueError(f"No SQLMesh configuration found at {path}")
    return next(iter(configs.values())), loaded_settings


def _resolve_gateway_and_schema(
    config: "Config",
    install_settings: t.Mapping[str, t.Any],
    gateway: t.Optional[str],
    schema: t.Optional[str],
) -> t.Tuple[str, str]:
    installed_gateway = install_settings.get("connection")
    resolved_gateway = (
        gateway
        or (installed_gateway if isinstance(installed_gateway, str) and installed_gateway else None)
        or config.default_gateway
    )
    if not resolved_gateway:
        gateways = list(config.gateways)
        if not gateways:
            raise ValueError("No gateway configured in SQLMesh config.")
        resolved_gateway = gateways[0]

    installed_schema = install_settings.get("schema")
    resolved_schema = (
        schema
        or (installed_schema if isinstance(installed_schema, str) and installed_schema else None)
        or DEFAULT_SCHEMA
    )
    return resolved_gateway, resolved_schema


def _read_environment_state(
    config: "Config",
    gateway: str,
    environment: str,
) -> t.Optional[_StateSnapshotInfo]:
    """Read active snapshot versions, owners, and columns via SQLMesh's read-only StateReader.

    Project state lives with the project's default gateway, so that is tried first. The log
    gateway is tried after it, for projects whose state shares the log gateway's connection.
    Returns ``None`` when no candidate has state for ``environment``.
    """
    candidates: t.List[str] = []
    for candidate in (config.default_gateway, gateway):
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        info = _read_state_from_gateway(config, candidate, environment)
        if info is not None:
            return info
    return None


def _read_state_from_gateway(
    config: "Config",
    gateway: str,
    environment: str,
) -> t.Optional[_StateSnapshotInfo]:
    from sqlmesh.core.state_sync import EngineAdapterStateSync

    _prepare_for_fork()
    state_connection = config.get_state_connection(gateway) or config.get_connection(gateway)
    adapter = state_connection.create_engine_adapter()
    try:
        reader = EngineAdapterStateSync(adapter, schema=config.get_state_schema(gateway))
        env = reader.get_environment(environment)
        if env is None or not env.snapshots:
            return None

        active_versions = frozenset(
            (snap.name, snap.version) for snap in env.snapshots if snap.version is not None
        )
        owners: t.Dict[str, t.Optional[str]] = {}
        columns: t.Dict[str, t.Tuple[t.Tuple[str, str], ...]] = {}
        try:
            snapshots = reader.get_snapshots(env.snapshots)
        except Exception:
            logger.debug(
                "sqlmesh-observability: failed to hydrate snapshots from state",
                exc_info=True,
            )
            snapshots = {}

        for snapshot in snapshots.values():
            owner, cols = _node_metadata(snapshot)
            owners[snapshot.name] = owner
            if cols:
                columns[snapshot.name] = cols
        return _StateSnapshotInfo(
            active_versions=active_versions, owners=owners, columns=columns, gateway=gateway
        )
    except Exception:
        logger.debug(
            "sqlmesh-observability: state lookup for environment '%s' on gateway '%s' unavailable",
            environment,
            gateway,
            exc_info=True,
        )
        return None
    finally:
        adapter.close()


def _prepare_for_fork() -> None:
    """Import pandas before SQLMesh reads snapshots from state.

    ``get_snapshots`` runs SQLMesh's fork-based process pool (``sqlmesh/core/snapshot/cache.py``).
    On macOS a child forked before pandas is loaded crashes in Objective-C initialization, and the
    read then fails, silently dropping owners and columns. SQLMesh's own ``Loader`` imports pandas
    first for the same reason. Imported here, not at module level (project rule).
    """
    import pandas  # noqa: F401


def _node_metadata(snapshot: t.Any) -> t.Tuple[t.Optional[str], t.Tuple[t.Tuple[str, str], ...]]:
    node = snapshot.node
    owner = getattr(node, "owner", None)
    cols_to_types = getattr(node, "columns_to_types", None) or {}
    columns = tuple(
        (str(col), kind.sql() if hasattr(kind, "sql") else str(kind))
        for col, kind in cols_to_types.items()
    )
    return owner, columns


def _hydrate_snapshots(
    config: "Config", gateway: str, wanted: t.Mapping[str, str]
) -> t.Tuple[t.Dict[str, t.Optional[str]], t.Dict[str, t.Tuple[t.Tuple[str, str], ...]]]:
    """Owner and columns for snapshots outside the environment, by ``{model_name: snapshot_id}``.

    Read-only, through the state reader, with the same gateway order as the environment read.
    A plan that stops before promotion has already stored its snapshots in state.
    """
    from sqlmesh.core.snapshot import SnapshotId
    from sqlmesh.core.state_sync import EngineAdapterStateSync

    owners: t.Dict[str, t.Optional[str]] = {}
    columns: t.Dict[str, t.Tuple[t.Tuple[str, str], ...]] = {}
    if not wanted:
        return owners, columns
    _prepare_for_fork()
    ids = [SnapshotId(name=name, identifier=identifier) for name, identifier in wanted.items()]
    candidates: t.List[str] = []
    for candidate in (config.default_gateway, gateway):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        try:
            connection = config.get_state_connection(candidate) or config.get_connection(candidate)
            adapter = connection.create_engine_adapter()
        except Exception:
            continue
        try:
            state = EngineAdapterStateSync(adapter, schema=config.get_state_schema(candidate))
            snapshots = state.get_snapshots(ids)
        except Exception:
            logger.debug("sqlmesh-observability: snapshot hydration failed", exc_info=True)
            snapshots = {}
        finally:
            adapter.close()
        for snapshot in snapshots.values():
            owner, cols = _node_metadata(snapshot)
            owners[snapshot.name] = owner
            if cols:
                columns[snapshot.name] = cols
        if snapshots:
            break
    return owners, columns


def _fetch_model_runs(
    adapter: "EngineAdapter",
    schema: str,
    active_versions: t.FrozenSet[t.Tuple[str, str]],
    latest_inv_id: t.Optional[str],
) -> t.Dict[str, t.List[RunRecord]]:
    """Scoped ``model_runs`` per model, oldest first."""
    runs_table = _table(schema, "model_runs")
    inv_table = _table(schema, "invocations")
    scope = _version_scope({v for _, v in active_versions}, latest_inv_id, "version")
    if scope is None or not adapter.table_exists(runs_table):
        return {}
    scope = scope.transform(
        lambda node: exp.column(node.name, table="r") if isinstance(node, exp.Column) else node
    )
    columns = (
        "model_name",
        "invocation_id",
        "snapshot_id",
        "version",
        "interval_start",
        "interval_end",
        "status",
        "duration_ms",
        "environment",
        "audit_only",
        "error_message",
    )
    query = (
        exp.select(
            *(exp.column(c, table="r") for c in columns), exp.column("started_at", table="i")
        )
        .from_(runs_table.as_("r"))
        .join(
            inv_table.as_("i"),
            on=exp.column("invocation_id", table="r").eq(exp.column("invocation_id", table="i")),
            join_type="left",
        )
        .where(scope)
    )
    runs: t.Dict[str, t.List[RunRecord]] = {}
    for row in adapter.fetchall(query):
        model_name, invocation_id, version = str(row[0]), str(row[1]), row[3]
        if not _pair_in_scope(model_name, version, invocation_id, active_versions, latest_inv_id):
            continue
        runs.setdefault(model_name, []).append(
            RunRecord(
                invocation_id=invocation_id,
                snapshot_id=None if row[2] is None else str(row[2]),
                version=None if version is None else str(version),
                interval_start=_fmt_ts(row[4]),
                interval_end=_fmt_ts(row[5]),
                status=None if row[6] is None else str(row[6]),
                duration_ms=None if row[7] is None else int(row[7]),
                environment=None if row[8] is None else str(row[8]),
                audit_only=bool(row[9]),
                error_message=None if row[10] is None else str(row[10]),
                started_at=_fmt_ts(row[11]),
            )
        )
    for items in runs.values():
        items.sort(key=lambda r: (r.started_at or "", r.invocation_id, r.interval_start or ""))
    return runs


_HEALTH_RANK = {"pass": 0, "skip": 1, "warn": 2, "fail": 3, "error": 4}


def _interval_health(
    runs: t.Sequence[RunRecord], audits: t.Sequence["AuditRecord"]
) -> t.Tuple[IntervalHealth, ...]:
    """Per interval, the latest run's worst outcome: error > fail > warn > skip > pass.

    A run that failed because its blocking audit failed is ``fail``; a run that failed with no
    failing audit is ``error`` (the model itself raised).
    """
    latest: t.Dict[str, RunRecord] = {}
    for run in runs:  # oldest first, so the last one wins
        if run.interval_start is not None:
            latest[run.interval_start] = run
    health: t.List[IntervalHealth] = []
    for start in sorted(latest):
        run = latest[start]
        mine = [
            a for a in audits if a.invocation_id == run.invocation_id and a.interval_start == start
        ]
        statuses = []
        for audit in mine:
            if audit.status == "error":
                statuses.append("error")
            elif audit.status == "fail":
                statuses.append("fail" if audit.blocking else "warn")
            elif audit.status == "skip":
                statuses.append("skip")
            else:
                statuses.append("pass")
        if run.status == "failure" and "fail" not in statuses:
            statuses.append("error")
        status = max(statuses or ["pass"], key=lambda s: _HEALTH_RANK[s])
        failing = [a.audit_name for a in mine if a.status in ("fail", "error")]
        parts = [f"run {run.status or 'unknown'}"]
        if mine:
            parts.append(f"{len(mine)} audit{'s' if len(mine) != 1 else ''}")
        if failing:
            parts.append("failing: " + ", ".join(sorted(set(failing))))
        if run.error_message and status == "error":
            parts.append(run.error_message.splitlines()[0][:200])
        health.append(IntervalHealth(interval_start=start, status=status, detail="; ".join(parts)))
    return tuple(health)


_RUN_LOG_LIMIT = 50


def _fetch_invocations(
    adapter: "EngineAdapter",
    schema: str,
    environment: str,
    in_scope_ids: t.Collection[str],
) -> t.List[InvocationRecord]:
    """Recent invocations that targeted ``environment`` or produced rows in scope, newest first."""
    table = _table(schema, "invocations")
    if not adapter.table_exists(table):
        return []
    conditions: t.List[exp.Expression] = [
        exp.column("environment").eq(exp.Literal.string(environment))
    ]
    ordered = sorted(set(in_scope_ids))
    for start in range(0, len(ordered), _IN_CHUNK):
        chunk = ordered[start : start + _IN_CHUNK]
        conditions.append(exp.column("invocation_id").isin(*(exp.Literal.string(i) for i in chunk)))
    query = (
        exp.select("invocation_id", "command", "environment", "status", "started_at", "finished_at")
        .from_(table)
        .where(exp.or_(*conditions))
        .order_by(exp.column("started_at").desc(), exp.column("invocation_id").desc())
        .limit(_RUN_LOG_LIMIT)
    )
    records: t.List[InvocationRecord] = []
    for row in adapter.fetchall(query):
        started, finished = _naive(row[4]), _naive(row[5])
        duration = (
            int((finished - started).total_seconds() * 1000) if started and finished else None
        )
        records.append(
            InvocationRecord(
                invocation_id=str(row[0]),
                command=None if row[1] is None else str(row[1]),
                environment=None if row[2] is None else str(row[2]),
                status=None if row[3] is None else str(row[3]),
                started_at=_fmt_ts(row[4]),
                finished_at=_fmt_ts(row[5]),
                duration_ms=duration,
            )
        )
    return records


def _with_counts(
    invocations: t.Sequence[InvocationRecord],
    runs: t.Mapping[str, t.Sequence[RunRecord]],
    audits: t.Sequence["AuditRecord"],
) -> t.Tuple[InvocationRecord, ...]:
    """Attach scoped model-run and audit counts to each invocation."""
    from dataclasses import replace

    counts: t.Dict[str, t.List[int]] = {}
    for run_list in runs.values():
        for run in run_list:
            c = counts.setdefault(run.invocation_id, [0, 0, 0, 0, 0])
            c[0] += 1
            c[1] += run.status == "failure"
    for audit in audits:
        c = counts.setdefault(audit.invocation_id, [0, 0, 0, 0, 0])
        c[2] += 1
        c[3] += audit.status in ("fail", "error")
        c[4] += audit.status == "error" or (audit.status == "fail" and bool(audit.blocking))
    empty = [0, 0, 0, 0, 0]
    return tuple(
        replace(
            inv,
            model_runs=counts.get(inv.invocation_id, empty)[0],
            failed_runs=counts.get(inv.invocation_id, empty)[1],
            audits=counts.get(inv.invocation_id, empty)[2],
            failing_audits=counts.get(inv.invocation_id, empty)[3],
            blocking_failures=counts.get(inv.invocation_id, empty)[4],
        )
        for inv in invocations
    )


def _latest_invocation_id(
    adapter: "EngineAdapter",
    schema: str,
    environment: str,
) -> t.Optional[str]:
    inv_table = _table(schema, "invocations")
    if not adapter.table_exists(inv_table):
        return None
    query = (
        exp.select(exp.column("invocation_id"))
        .from_(inv_table)
        .where(exp.column("environment").eq(exp.Literal.string(environment)))
        .order_by(exp.column("started_at").desc(), exp.column("invocation_id").desc())
        .limit(1)
    )
    row = adapter.fetchone(query)
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _scope_condition(
    versions: t.Collection[str],
    latest_inv_id: t.Optional[str],
    environment: str,
) -> exp.Condition:
    """SQL pre-filter matching :func:`_in_scope`; the exact (model, version) check runs after.

    Versions are fingerprints, so ``version IN (...)`` is already nearly exact and keeps the scan
    to the environment's rows instead of the whole history.
    """
    version_col = exp.column("version", table="a")
    conditions: t.List[exp.Expression] = []
    ordered = sorted(versions)
    for start in range(0, len(ordered), _IN_CHUNK):
        chunk = ordered[start : start + _IN_CHUNK]
        conditions.append(version_col.isin(*(exp.Literal.string(v) for v in chunk)))
    if latest_inv_id is not None:
        conditions.append(
            exp.column("invocation_id", table="a").eq(exp.Literal.string(latest_inv_id))
        )
    if not versions and latest_inv_id is None:
        conditions.append(exp.column("environment", table="a").eq(exp.Literal.string(environment)))
    return exp.or_(*conditions)


def _fetch_audit_rows(
    adapter: "EngineAdapter",
    schema: str,
    scope: t.Optional[exp.Condition] = None,
) -> t.List[AuditRecord]:
    audit_tbl = _table(schema, "audit_results").as_("a")
    inv_tbl = _table(schema, "invocations")
    has_invocations = adapter.table_exists(inv_tbl)

    started_col = (
        exp.column("started_at", table="i")
        if has_invocations
        else exp.alias_(exp.null(), "started_at")
    )
    query = exp.select(
        exp.column("invocation_id", table="a"),
        exp.column("model_name", table="a"),
        exp.column("snapshot_id", table="a"),
        exp.column("version", table="a"),
        exp.column("physical_table", table="a"),
        exp.column("environment", table="a"),
        exp.column("audit_name", table="a"),
        exp.column("args_json", table="a"),
        exp.column("interval_start", table="a"),
        exp.column("interval_end", table="a"),
        exp.column("status", table="a"),
        exp.column("violation_count", table="a"),
        exp.column("blocking", table="a"),
        exp.column("query_sql", table="a"),
        exp.column("error_message", table="a"),
        started_col,
    ).from_(audit_tbl)
    if has_invocations:
        query = query.join(
            inv_tbl.as_("i"),
            on=exp.column("invocation_id", table="a").eq(exp.column("invocation_id", table="i")),
            join_type="left",
        )
    if scope is not None:
        query = query.where(scope)

    records: t.List[AuditRecord] = []
    for row in adapter.fetchall(query):
        records.append(
            AuditRecord(
                invocation_id=str(row[0]),
                model_name=str(row[1]),
                snapshot_id=str(row[2]) if row[2] is not None else None,
                version=str(row[3]) if row[3] is not None else None,
                physical_table=str(row[4]) if row[4] is not None else None,
                environment=str(row[5]) if row[5] is not None else None,
                audit_name=str(row[6]),
                args_json=str(row[7]) if row[7] is not None else None,
                interval_start=_fmt_ts(row[8]),
                interval_end=_fmt_ts(row[9]),
                status=str(row[10]),
                violation_count=int(row[11]) if row[11] is not None else None,
                blocking=bool(row[12]) if row[12] is not None else None,
                query_sql=str(row[13]) if row[13] is not None else None,
                error_message=str(row[14]) if row[14] is not None else None,
                started_at=_fmt_ts(row[15]),
            )
        )
    return records


def _fetch_samples(
    adapter: "EngineAdapter",
    schema: str,
    invocation_ids: t.Sequence[str],
) -> t.Dict[t.Tuple[str, str, str, t.Optional[str]], t.Tuple[str, ...]]:
    if not invocation_ids:
        return {}
    samples_table = _table(schema, "audit_samples")
    if not adapter.table_exists(samples_table):
        return {}

    grouped: t.Dict[t.Tuple[str, str, str, t.Optional[str]], t.List[str]] = {}
    unique_ids = sorted(set(invocation_ids))
    for start in range(0, len(unique_ids), _IN_CHUNK):
        chunk = unique_ids[start : start + _IN_CHUNK]
        query = (
            exp.select(
                exp.column("invocation_id"),
                exp.column("model_name"),
                exp.column("audit_name"),
                exp.column("interval_start"),
                exp.column("row_json"),
            )
            .from_(samples_table)
            .where(exp.column("invocation_id").isin(*(exp.Literal.string(i) for i in chunk)))
        )
        for row in adapter.fetchall(query):
            key = (str(row[0]), str(row[1]), str(row[2]), _fmt_ts(row[3]))
            grouped.setdefault(key, []).append(str(row[4]))

    return {key: tuple(values) for key, values in grouped.items()}


def _version_scope(
    versions: t.Collection[str], latest_inv_id: t.Optional[str], version_column: str
) -> t.Optional[exp.Condition]:
    conditions: t.List[exp.Expression] = []
    ordered = sorted(versions)
    for start in range(0, len(ordered), _IN_CHUNK):
        chunk = ordered[start : start + _IN_CHUNK]
        conditions.append(exp.column(version_column).isin(*(exp.Literal.string(v) for v in chunk)))
    if latest_inv_id is not None:
        conditions.append(exp.column("invocation_id").eq(exp.Literal.string(latest_inv_id)))
    return exp.or_(*conditions) if conditions else None


def _pair_in_scope(
    model_name: str,
    version: t.Optional[str],
    invocation_id: str,
    active_versions: t.FrozenSet[t.Tuple[str, str]],
    latest_inv_id: t.Optional[str],
) -> bool:
    return (model_name, version or "") in active_versions or invocation_id == latest_inv_id


def _naive(value: t.Any) -> t.Optional[datetime.datetime]:
    from sqlmesh_observability.monitors import _naive_utc

    try:
        return _naive_utc(value)
    except Exception:
        return None


def _fetch_metric_series(
    adapter: "EngineAdapter",
    schema: str,
    active_versions: t.FrozenSet[t.Tuple[str, str]],
    latest_inv_id: t.Optional[str],
    monitor_settings: t.Any,
) -> t.Dict[str, t.Tuple[MetricSeries, ...]]:
    """Scoped metric series per model: latest value per interval, scored at read time."""
    from sqlmesh_observability.monitors import score_series

    table = _table(schema, "metrics")
    scope = _version_scope({v for _, v in active_versions}, latest_inv_id, "version")
    if scope is None or not adapter.table_exists(table):
        return {}
    query = (
        exp.select(
            "model_name",
            "version",
            "invocation_id",
            "interval_start",
            "metric_name",
            "column_name",
            "metric_value",
            "updated_at",
            "error_message",
        )
        .from_(table)
        .where(scope)
    )
    # (model, metric, column) -> interval_start -> (updated_at, value, error, invocation_id)
    latest: t.Dict[
        t.Tuple[str, str, str],
        t.Dict[
            datetime.datetime,
            t.Tuple[datetime.datetime, t.Optional[float], t.Optional[str], str],
        ],
    ] = {}
    for row in adapter.fetchall(query):
        model_name, version, invocation_id, start, metric, column, value, updated, error = row
        if not _pair_in_scope(
            str(model_name), version, str(invocation_id), active_versions, latest_inv_id
        ):
            continue
        when = _naive(start)
        if when is None:
            continue
        stamp = _naive(updated) or datetime.datetime.min
        key = (str(model_name), str(metric), str(column or ""))
        by_interval = latest.setdefault(key, {})
        if when not in by_interval or stamp > by_interval[when][0]:
            by_interval[when] = (
                stamp,
                None if value is None else float(value),
                None if error is None else str(error),
                str(invocation_id),
            )

    series: t.Dict[str, t.List[MetricSeries]] = {}
    for (model_name, metric, column), by_start in sorted(latest.items()):
        ordered = sorted(by_start.items())
        scored = score_series(
            [(when, value) for when, (_, value, _, _) in ordered],
            monitor_settings.score_for(metric),
        )
        points = tuple(
            MetricPoint(
                interval_start=_fmt_ts(p.interval_start) or "",
                value=p.value,
                mean=p.mean,
                stddev=p.stddev,
                training_points=p.training_points,
                anomaly=p.anomaly,
                error_message=error,
                invocation_id=invocation_id,
            )
            for p, (_, (_, _, error, invocation_id)) in zip(scored, ordered)
        )
        series.setdefault(model_name, []).append(
            MetricSeries(
                metric_name=metric,
                column_name=column,
                points=points,
                threshold=float(monitor_settings.score_for(metric).threshold),
            )
        )
    return {name: tuple(items) for name, items in series.items()}


def _fetch_schema_changes(
    adapter: "EngineAdapter",
    schema: str,
    active_versions: t.FrozenSet[t.Tuple[str, str]],
    latest_inv_id: t.Optional[str],
) -> t.Dict[str, t.Tuple[SchemaChange, ...]]:
    table = _table(schema, "schema_changes")
    scope = _version_scope({v for _, v in active_versions}, latest_inv_id, "to_version")
    if scope is None or not adapter.table_exists(table):
        return {}
    query = (
        exp.select(
            "model_name",
            "to_version",
            "invocation_id",
            "column_name",
            "change_type",
            "old_type",
            "new_type",
            "from_version",
            "from_snapshot_id",
            "to_snapshot_id",
            "recorded_at",
        )
        .from_(table)
        .where(scope)
    )
    changes: t.Dict[str, t.List[t.Tuple[str, SchemaChange]]] = {}
    for row in adapter.fetchall(query):
        model_name, to_version, invocation_id = str(row[0]), row[1], str(row[2])
        if not _pair_in_scope(
            model_name, to_version, invocation_id, active_versions, latest_inv_id
        ):
            continue
        recorded_at = _fmt_ts(row[10])
        change = SchemaChange(
            column_name=str(row[3]),
            change_type=str(row[4]),
            old_type=None if row[5] is None else str(row[5]),
            new_type=None if row[6] is None else str(row[6]),
            from_version=None if row[7] is None else str(row[7]),
            to_version=None if to_version is None else str(to_version),
            from_snapshot_id=None if row[8] is None else str(row[8]),
            to_snapshot_id=None if row[9] is None else str(row[9]),
            recorded_at=recorded_at,
        )
        changes.setdefault(model_name, []).append((recorded_at or "", change))
    return {
        name: tuple(
            c for _, c in sorted(items, key=lambda i: (i[0], i[1].column_name), reverse=True)
        )
        for name, items in changes.items()
    }


def _in_scope(
    record: AuditRecord,
    environment: str,
    active_versions: t.FrozenSet[t.Tuple[str, str]],
    latest_inv_id: t.Optional[str],
) -> bool:
    if record.version is not None and (record.model_name, record.version) in active_versions:
        return True
    if latest_inv_id is not None and record.invocation_id == latest_inv_id:
        return True
    if not active_versions and latest_inv_id is None:
        return record.environment == environment
    return False


def audit_key(record: AuditRecord) -> t.Tuple[str, str, t.Optional[str], t.Optional[str]]:
    """Identity of one audit use on one interval.

    A model may list the same audit twice with different arguments, so the arguments are part of
    the key: without them one use's result would hide the other's.
    """
    return (record.model_name, record.audit_name, record.args_json, record.interval_start)


def _is_warning(record: AuditRecord) -> bool:
    return record.status == "fail" and not record.blocking


def _is_overview_failure(record: AuditRecord) -> bool:
    if record.status == "error":
        return True
    return record.status == "fail" and bool(record.blocking)


def _sort_key(record: AuditRecord) -> t.Tuple[str, str, str, str, str]:
    return (
        record.interval_start or "",
        record.started_at or "",
        record.invocation_id,
        record.audit_name,
        record.args_json or "",
    )


def _build_report_data(
    records: t.Sequence[AuditRecord],
    state_info: t.Optional[_StateSnapshotInfo],
    environment: str,
    schema: str,
    gateway: str,
    metrics: t.Optional[t.Mapping[str, t.Tuple[MetricSeries, ...]]] = None,
    schema_changes: t.Optional[t.Mapping[str, t.Tuple[SchemaChange, ...]]] = None,
    runs: t.Optional[t.Mapping[str, t.Sequence[RunRecord]]] = None,
    extra_owners: t.Optional[t.Mapping[str, t.Optional[str]]] = None,
    extra_columns: t.Optional[t.Mapping[str, t.Tuple[t.Tuple[str, str], ...]]] = None,
    invocations: t.Sequence[InvocationRecord] = (),
    monitors: t.Tuple[str, ...] = (),
) -> ReportData:
    by_model: t.Dict[str, t.List[AuditRecord]] = {}
    for record in records:
        by_model.setdefault(record.model_name, []).append(record)

    models: t.List[ModelReport] = []
    failing_models: t.List[ModelReport] = []
    warning_models: t.List[ModelReport] = []
    passing_audit_count = 0
    owners = {**(extra_owners or {}), **(state_info.owners if state_info else {})}
    columns = {**(extra_columns or {}), **(state_info.columns if state_info else {})}

    metrics = metrics or {}
    schema_changes = schema_changes or {}
    runs = runs or {}
    anomalies: t.List[MetricAnomaly] = []
    for model_name in sorted(set(by_model) | set(metrics) | set(schema_changes) | set(runs)):
        model_audits = sorted(by_model.get(model_name, ()), key=_sort_key, reverse=True)
        for series in metrics.get(model_name, ()):
            if series.points and series.points[-1].anomaly:
                anomalies.append(
                    MetricAnomaly(
                        model_name=model_name,
                        metric_name=series.metric_name,
                        column_name=series.column_name,
                        point=series.points[-1],
                    )
                )

        # Latest run per audit use and interval, for Overview counts/status.
        latest_by_key: t.Dict[t.Tuple[str, str, t.Optional[str], t.Optional[str]], AuditRecord] = {}
        for audit in model_audits:
            dedup_key = audit_key(audit)
            existing = latest_by_key.get(dedup_key)
            if existing is None or (audit.started_at or "", audit.invocation_id) > (
                existing.started_at or "",
                existing.invocation_id,
            ):
                latest_by_key[dedup_key] = audit

        latest_audits = list(latest_by_key.values())
        passing_audit_count += sum(1 for a in latest_audits if a.status == "pass")
        failing_blocking = tuple(
            sorted(
                (a for a in latest_audits if _is_overview_failure(a)),
                key=_sort_key,
                reverse=True,
            )
        )
        warnings = tuple(
            sorted((a for a in latest_audits if _is_warning(a)), key=_sort_key, reverse=True)
        )
        model_report = ModelReport(
            model_name=model_name,
            owner=owners.get(model_name),
            columns=columns.get(model_name, ()),
            audits=tuple(model_audits),
            failing_blocking_audits=failing_blocking,
            warning_audits=warnings,
            metrics=metrics.get(model_name, ()),
            schema_changes=schema_changes.get(model_name, ()),
            runs=tuple(runs.get(model_name, ())),
            health=_interval_health(runs.get(model_name, ()), model_audits),
        )
        models.append(model_report)
        if failing_blocking:
            failing_models.append(model_report)
        if warnings:
            warning_models.append(model_report)

    incidents = tuple(
        sorted(
            (r for r in records if r.status in ("fail", "error")),
            key=_sort_key,
            reverse=True,
        )
    )
    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return ReportData(
        environment=environment,
        schema=schema,
        gateway=gateway,
        generated_at=generated_at,
        failing_model_count=len(failing_models),
        passing_audit_count=passing_audit_count,
        failing_models=tuple(failing_models),
        models=tuple(models),
        incidents=incidents,
        warning_model_count=len(warning_models),
        warning_models=tuple(warning_models),
        state_available=state_info is not None,
        state_gateway=state_info.gateway if state_info else None,
        anomalies=tuple(anomalies),
        invocations=_with_counts(invocations, runs, records),
        monitors=monitors,
    )


def _configured_monitors(settings: t.Any) -> t.Tuple[str, ...]:
    """Names of the passive monitors ``install(monitors=...)`` turned on, in a fixed order."""
    on = {
        "volume": settings.volume is not None,
        "freshness": settings.freshness is not None,
        "schema": bool(settings.schema),
        "columns": bool(settings.columns),
    }
    return tuple(name for name, enabled in on.items() if enabled)


def load_report_data(
    path: t.Union[str, Path] = ".",
    gateway: t.Optional[str] = None,
    schema: t.Optional[str] = None,
    environment: str = DEFAULT_ENVIRONMENT,
) -> ReportData:
    """Load scoped observability data for ``environment`` from the warehouse and SQLMesh state."""
    config, install_settings = _load_project_config(path)
    return load_report_data_for(config, install_settings, gateway, schema, environment)


def load_report_data_for(
    config: "Config",
    install_settings: t.Mapping[str, t.Any],
    gateway: t.Optional[str] = None,
    schema: t.Optional[str] = None,
    environment: str = DEFAULT_ENVIRONMENT,
) -> ReportData:
    """:func:`load_report_data` for a ``config.py`` that is already loaded."""
    resolved_gateway, resolved_schema = _resolve_gateway_and_schema(
        config, install_settings, gateway, schema
    )

    adapter = config.get_connection(resolved_gateway).create_engine_adapter()
    try:
        if not adapter.table_exists(_table(resolved_schema, "audit_results")):
            raise MissingAuditTableError(resolved_schema)

        latest_inv_id = _latest_invocation_id(adapter, resolved_schema, environment)
        state_info = _read_environment_state(config, resolved_gateway, environment)
        active_versions = state_info.active_versions if state_info else frozenset()
        scope = _scope_condition({v for _, v in active_versions}, latest_inv_id, environment)
        scoped = [
            row
            for row in _fetch_audit_rows(adapter, resolved_schema, scope)
            if _in_scope(row, environment, active_versions, latest_inv_id)
        ]

        from sqlmesh_observability.monitors import normalize_monitors

        monitor_settings = normalize_monitors(install_settings.get("monitors"))
        metric_series = _fetch_metric_series(
            adapter, resolved_schema, active_versions, latest_inv_id, monitor_settings
        )
        changes = _fetch_schema_changes(adapter, resolved_schema, active_versions, latest_inv_id)
        runs = _fetch_model_runs(adapter, resolved_schema, active_versions, latest_inv_id)
        in_scope_ids = {r.invocation_id for r in scoped} | {
            run.invocation_id for run_list in runs.values() for run in run_list
        }
        invocations = _fetch_invocations(adapter, resolved_schema, environment, in_scope_ids)

        samples_map = _fetch_samples(
            adapter,
            resolved_schema,
            [r.invocation_id for r in scoped if r.status == "fail"],
        )
        hydrated = [
            AuditRecord(
                invocation_id=r.invocation_id,
                model_name=r.model_name,
                snapshot_id=r.snapshot_id,
                version=r.version,
                physical_table=r.physical_table,
                environment=r.environment,
                audit_name=r.audit_name,
                args_json=r.args_json,
                interval_start=r.interval_start,
                interval_end=r.interval_end,
                status=r.status,
                violation_count=r.violation_count,
                blocking=r.blocking,
                query_sql=r.query_sql,
                error_message=r.error_message,
                started_at=r.started_at,
                samples=samples_map.get(
                    (r.invocation_id, r.model_name, r.audit_name, r.interval_start), ()
                ),
            )
            for r in scoped
        ]
    finally:
        adapter.close()

    # Owner and columns for models that ran but whose snapshot is not in the environment.
    known = state_info.owners if state_info else {}
    wanted: t.Dict[str, str] = {}
    for model_name, run_list in runs.items():
        for run in run_list:  # oldest first: the newest snapshot that ran wins
            if run.snapshot_id:
                wanted[model_name] = run.snapshot_id
    for record in hydrated:
        if record.snapshot_id and record.model_name not in wanted:
            wanted[record.model_name] = record.snapshot_id
    wanted = {name: sid for name, sid in wanted.items() if name not in known}
    extra_owners, extra_columns = _hydrate_snapshots(config, resolved_gateway, wanted)

    return _build_report_data(
        hydrated,
        state_info=state_info,
        environment=environment,
        schema=resolved_schema,
        gateway=resolved_gateway,
        metrics=metric_series,
        schema_changes=changes,
        runs=runs,
        extra_owners=extra_owners,
        extra_columns=extra_columns,
        invocations=invocations,
        monitors=_configured_monitors(monitor_settings),
    )
