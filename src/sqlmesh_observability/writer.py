"""Writes finished invocations to the package's tables in the warehouse.

One writer thread owns all warehouse I/O. The notification target's end/failure hook submits a
finished :class:`~sqlmesh_observability.collector.Invocation` and waits, bounded, until it is
written. That way a short CLI process persists before it exits, and a long-lived ``sqlmesh ui``
process writes each apply as it finishes. ``atexit`` drains anything left, as a backstop only.
"""

from __future__ import annotations

import atexit
import datetime
import json
import logging
import queue
import threading
import typing as t

from sqlglot import exp

from sqlmesh_observability import monitors
from sqlmesh_observability.logtables import LogTables
from sqlmesh_observability.monitors import MonitorSettings, RecordedSchema

if t.TYPE_CHECKING:
    from sqlmesh.core.config import Config
    from sqlmesh.core.engine_adapter import EngineAdapter

    from sqlmesh_observability.collector import Invocation

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2  # 2: metrics, schema_columns, schema_changes
_INSERT_CHUNK = 500


def _types(**columns: str) -> t.Dict[str, exp.DataType]:
    return {name: exp.DataType.build(kind) for name, kind in columns.items()}


TABLES: t.Dict[str, t.Dict[str, exp.DataType]] = {
    "schema_version": _types(version="INT"),
    "invocations": _types(
        invocation_id="TEXT",
        command="TEXT",
        environment="TEXT",
        plan_id="TEXT",
        started_at="TIMESTAMP",
        finished_at="TIMESTAMP",
        status="TEXT",
        sqlmesh_version="TEXT",
    ),
    "model_runs": _types(
        invocation_id="TEXT",
        model_name="TEXT",
        snapshot_id="TEXT",
        version="TEXT",
        physical_table="TEXT",
        interval_start="TIMESTAMP",
        interval_end="TIMESTAMP",
        batch_index="INT",
        execution_time="TIMESTAMP",
        environment="TEXT",
        audit_only="BOOLEAN",
        status="TEXT",
        duration_ms="BIGINT",
        error_message="TEXT",
    ),
    "audit_results": _types(
        invocation_id="TEXT",
        model_name="TEXT",
        snapshot_id="TEXT",
        version="TEXT",
        physical_table="TEXT",
        environment="TEXT",
        audit_name="TEXT",
        args_json="TEXT",
        interval_start="TIMESTAMP",
        interval_end="TIMESTAMP",
        status="TEXT",
        violation_count="BIGINT",
        blocking="BOOLEAN",
        query_sql="TEXT",
        error_message="TEXT",
    ),
    "audit_samples": _types(
        invocation_id="TEXT",
        model_name="TEXT",
        audit_name="TEXT",
        interval_start="TIMESTAMP",
        row_json="TEXT",
    ),
    "metrics": _types(
        invocation_id="TEXT",
        model_name="TEXT",
        version="TEXT",
        physical_table="TEXT",
        interval_start="TIMESTAMP",
        interval_end="TIMESTAMP",
        metric_name="TEXT",
        column_name="TEXT",  # '' for table-level metrics, never NULL
        metric_value="DOUBLE",
        updated_at="TIMESTAMP",
        error_message="TEXT",
    ),
    "schema_columns": _types(
        invocation_id="TEXT",
        model_name="TEXT",
        snapshot_id="TEXT",
        version="TEXT",
        ordinal="INT",
        column_name="TEXT",
        data_type="TEXT",
        recorded_at="TIMESTAMP",
    ),
    "schema_changes": _types(
        invocation_id="TEXT",
        model_name="TEXT",
        from_snapshot_id="TEXT",
        to_snapshot_id="TEXT",
        from_version="TEXT",
        to_version="TEXT",
        column_name="TEXT",
        change_type="TEXT",
        old_type="TEXT",
        new_type="TEXT",
        recorded_at="TIMESTAMP",
    ),
}
INVOCATION_TABLES = ("invocations", "model_runs", "audit_results", "audit_samples")


def _ms_to_datetime(value: t.Optional[int]) -> t.Optional[datetime.datetime]:
    if value is None:
        return None
    return datetime.datetime.fromtimestamp(value / 1000, tz=datetime.timezone.utc)


def _literal(value: t.Any, kind: exp.DataType) -> exp.Expression:
    if value is None:
        return exp.cast(exp.null(), kind)
    if isinstance(value, bool):
        return exp.cast(exp.Boolean(this=value), kind)
    if isinstance(value, (int, float)):
        return exp.cast(exp.Literal.number(value), kind)
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return exp.cast(exp.Literal.string(value.isoformat(sep=" ")), kind)
    return exp.cast(exp.Literal.string(str(value)), kind)


def _sqlmesh_version() -> str:
    try:
        from sqlmesh import __version__

        return str(__version__)
    except Exception:
        return "unknown"


class Writer:
    def __init__(
        self,
        config: "Config",
        gateway: str,
        schema: str = "observability",
        sample_rows: int = 0,
        flush_timeout: float = 60.0,
        monitors: t.Optional[MonitorSettings] = None,
    ) -> None:
        self.config = config
        self.monitors = monitors or MonitorSettings()
        self.gateway = gateway
        self.schema = schema
        self.sample_rows = sample_rows
        self.flush_timeout = flush_timeout
        # (invocation, done); an invocation of None is a drain marker.
        self._queue: "queue.Queue[t.Tuple[t.Optional[Invocation], threading.Event]]" = queue.Queue()
        self._thread: t.Optional[threading.Thread] = None
        self._start_lock = threading.Lock()
        self._tables_ready = False
        atexit.register(self.drain)

    def settings(self) -> t.Tuple[str, str, int, MonitorSettings]:
        return (self.gateway, self.schema, self.sample_rows, self.monitors)

    # --- Submission ------------------------------------------------------------------------

    def submit_and_wait(self, invocation: "Invocation", timeout: t.Optional[float] = None) -> bool:
        """Queue ``invocation`` and wait until it is written. False if the wait timed out."""
        done = threading.Event()
        self._ensure_thread()
        self._queue.put((invocation, done))
        return done.wait(self.flush_timeout if timeout is None else timeout)

    def drain(self, timeout: float = 30.0) -> None:
        """Wait for queued invocations to be written (``atexit`` backstop)."""
        if self._thread is None or not self._thread.is_alive():
            return
        finished = threading.Event()
        self._queue.put((None, finished))  # set once everything queued before it is processed
        finished.wait(timeout)

    def stop(self) -> None:
        self.drain(timeout=5.0)

    def _ensure_thread(self) -> None:
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="sqlmesh-observability-writer", daemon=True
                )
                self._thread.start()

    def _run(self) -> None:
        while True:
            invocation, done = self._queue.get()
            if invocation is None:
                done.set()
                continue
            try:
                self.write(invocation)
            except Exception:
                logger.debug("sqlmesh-observability: writing invocation failed", exc_info=True)
            finally:
                done.set()

    # --- Writing ---------------------------------------------------------------------------

    def _adapter(self) -> "EngineAdapter":
        return self.config.get_connection(self.gateway).create_engine_adapter()

    def _sample_adapter(self) -> "EngineAdapter":
        return self.config.get_connection().create_engine_adapter()

    def _table(self, name: str) -> exp.Table:
        return exp.to_table(f"{self.schema}.{name}")

    def ensure_tables(self, adapter: LogTables) -> None:
        if self._tables_ready:
            return
        adapter.create_schema(self.schema)
        for name, columns in TABLES.items():
            adapter.create_table(self._table(name), columns)
        version_table = self._table("schema_version")
        row = adapter.fetchone(exp.select("MAX(version)").from_(version_table))
        current = row[0] if row else None
        if current is None or int(current) < SCHEMA_VERSION:
            # Upgrades so far only add tables, which CREATE TABLE IF NOT EXISTS has just done.
            with adapter.transaction():
                adapter.delete_from(version_table, exp.true())
                self._insert(adapter, "schema_version", [{"version": SCHEMA_VERSION}])
        self._tables_ready = True

    def write(self, invocation: "Invocation") -> None:
        """Write one invocation, replacing any rows an earlier write of it left (idempotent)."""
        rows = self._rows(invocation)
        rows["audit_samples"] = self._samples(invocation)
        adapter = self._adapter()
        log = LogTables(adapter)
        self.ensure_tables(log)
        where = exp.column("invocation_id").eq(exp.Literal.string(invocation.invocation_id))
        with log.transaction():
            for name in INVOCATION_TABLES:
                log.delete_from(self._table(name), where)
            for name in INVOCATION_TABLES:
                self._insert(log, name, rows[name])
        # Base rows are committed above; monitors run after, so a slow or failing metric query
        # can lose only metrics.
        if self.monitors.enabled:
            self._write_monitors(invocation, adapter, log)

    # --- Passive monitors -----------------------------------------------------------------------

    def _write_monitors(
        self, invocation: "Invocation", adapter: "EngineAdapter", log: LogTables
    ) -> None:
        """Metrics query model tables through ``adapter``; results go to ``log``."""
        settings = self.monitors
        if settings.collects_metrics:
            try:
                rows = monitors.collect_metrics(
                    invocation, self._sample_adapter(), adapter, settings
                )
                self._replace_metrics(log, rows)
            except Exception:
                logger.debug("sqlmesh-observability: passive metrics failed", exc_info=True)
        if settings.schema and invocation.schemas:
            try:
                self._write_schemas(invocation, log)
            except Exception:
                logger.debug("sqlmesh-observability: schema capture failed", exc_info=True)

    def _replace_metrics(self, adapter: LogTables, rows: t.List[t.Dict[str, t.Any]]) -> None:
        """Latest wins per (model, version, interval_start, metric, column)."""
        if not rows:
            return
        groups: t.Dict[t.Tuple[str, str, str, str], t.List[t.Any]] = {}
        for row in rows:
            key = (row["model_name"], row["version"], row["metric_name"], row["column_name"])
            groups.setdefault(key, []).append(row["interval_start"])
        timestamp = TABLES["metrics"]["interval_start"]
        table = self._table("metrics")
        with adapter.transaction():
            for (model_name, version, metric_name, column_name), starts in groups.items():
                where = exp.and_(
                    exp.column("model_name").eq(exp.Literal.string(model_name)),
                    exp.column("version").eq(exp.Literal.string(version)),
                    exp.column("metric_name").eq(exp.Literal.string(metric_name)),
                    exp.column("column_name").eq(exp.Literal.string(column_name)),
                )
                known = [s for s in starts if s is not None]
                matches: t.List[exp.Expression] = []
                if known:
                    matches.append(
                        exp.column("interval_start").isin(*(_literal(s, timestamp) for s in known))
                    )
                if len(known) < len(starts):
                    matches.append(exp.column("interval_start").is_(exp.null()))
                adapter.delete_from(table, exp.and_(where, exp.or_(*matches)))
            self._insert(adapter, "metrics", rows)

    def _recorded_schemas(
        self, adapter: LogTables, model_names: t.Iterable[str]
    ) -> t.Dict[str, t.Dict[str, RecordedSchema]]:
        names = sorted(set(model_names))
        query = (
            exp.select(
                "model_name",
                "snapshot_id",
                "version",
                "recorded_at",
                "ordinal",
                "column_name",
                "data_type",
            )
            .from_(self._table("schema_columns"))
            .where(exp.column("model_name").isin(*(exp.Literal.string(n) for n in names)))
            .order_by("model_name", "snapshot_id", "ordinal")
        )
        columns: t.Dict[t.Tuple[str, str], t.List[t.Tuple[str, str]]] = {}
        meta: t.Dict[t.Tuple[str, str], t.Tuple[str, datetime.datetime]] = {}
        for model_name, snapshot_id, version, recorded_at, _, column, kind in adapter.fetchall(
            query
        ):
            key = (str(model_name), str(snapshot_id))
            columns.setdefault(key, []).append((str(column), str(kind)))
            meta[key] = (str(version), monitors._naive_utc(recorded_at) or datetime.datetime.min)
        recorded: t.Dict[str, t.Dict[str, RecordedSchema]] = {}
        for (model_name, snapshot_id), cols in columns.items():
            version, recorded_at = meta[(model_name, snapshot_id)]
            recorded.setdefault(model_name, {})[snapshot_id] = RecordedSchema(
                snapshot_id=snapshot_id,
                version=version,
                recorded_at=recorded_at,
                columns=tuple(cols),
            )
        return recorded

    def _write_schemas(self, invocation: "Invocation", adapter: LogTables) -> None:
        recorded = self._recorded_schemas(adapter, (s.model_name for s in invocation.schemas))
        # A replay of a capture already recorded is compared with its ancestors, not itself.
        for capture in invocation.schemas:
            recorded.get(capture.model_name, {}).pop(capture.snapshot_id, None)
        column_rows, change_rows = monitors.schema_rows(invocation, recorded)
        with adapter.transaction():
            for capture in invocation.schemas:
                model = exp.column("model_name").eq(exp.Literal.string(capture.model_name))
                snapshot = exp.Literal.string(capture.snapshot_id)
                adapter.delete_from(
                    self._table("schema_columns"),
                    exp.and_(model, exp.column("snapshot_id").eq(snapshot)),
                )
                adapter.delete_from(
                    self._table("schema_changes"),
                    exp.and_(model, exp.column("to_snapshot_id").eq(snapshot)),
                )
            self._insert(adapter, "schema_columns", column_rows)
            self._insert(adapter, "schema_changes", change_rows)

    def _insert(self, adapter: LogTables, name: str, rows: t.List[t.Dict[str, t.Any]]) -> None:
        columns = TABLES[name]
        for start in range(0, len(rows), _INSERT_CHUNK):
            chunk = rows[start : start + _INSERT_CHUNK]
            values = exp.values(
                [
                    tuple(_literal(row.get(column), kind) for column, kind in columns.items())
                    for row in chunk
                ],
                alias="_rows",
                columns=list(columns),
            )
            query = exp.select(*(exp.column(c) for c in columns)).from_(values)
            adapter.insert_append(self._table(name), query, columns)

    def _rows(self, invocation: "Invocation") -> t.Dict[str, t.List[t.Dict[str, t.Any]]]:
        return {
            "invocations": [
                {
                    "invocation_id": invocation.invocation_id,
                    "command": invocation.command,
                    "environment": invocation.environment,
                    "plan_id": invocation.plan_id,
                    "started_at": invocation.started_at,
                    "finished_at": invocation.finished_at,
                    "status": invocation.status,
                    "sqlmesh_version": _sqlmesh_version(),
                }
            ],
            "model_runs": [
                {
                    "invocation_id": m.invocation_id,
                    "model_name": m.model_name,
                    "snapshot_id": m.snapshot_id,
                    "version": m.version,
                    "physical_table": m.physical_table,
                    "interval_start": _ms_to_datetime(m.interval_start),
                    "interval_end": _ms_to_datetime(m.interval_end),
                    "batch_index": m.batch_index,
                    "execution_time": _ms_to_datetime(m.execution_time),
                    "environment": m.environment,
                    "audit_only": m.audit_only,
                    "status": m.status,
                    "duration_ms": m.duration_ms,
                    "error_message": m.error_message,
                }
                for m in invocation.models
            ],
            "audit_results": [
                {
                    "invocation_id": a.invocation_id,
                    "model_name": a.model_name,
                    "snapshot_id": a.snapshot_id,
                    "version": a.version,
                    "physical_table": a.physical_table,
                    "environment": a.environment,
                    "audit_name": a.audit_name,
                    "args_json": a.args_json,
                    "interval_start": _ms_to_datetime(a.interval_start),
                    "interval_end": _ms_to_datetime(a.interval_end),
                    "status": a.status,
                    "violation_count": a.violation_count,
                    "blocking": a.blocking,
                    "query_sql": a.query_sql,
                    "error_message": a.error_message,
                }
                for a in invocation.audits
            ],
        }

    def _samples(self, invocation: "Invocation") -> t.List[t.Dict[str, t.Any]]:
        if self.sample_rows <= 0:
            return []
        failing = [a for a in invocation.audits if a.status == "fail" and a.query_sql]
        if not failing:
            return []
        samples: t.List[t.Dict[str, t.Any]] = []
        adapter = self._sample_adapter()
        for audit in failing:
            try:
                import sqlglot

                query = sqlglot.parse_one(audit.query_sql or "", read=adapter.dialect)
                if not isinstance(query, exp.Query):
                    continue
                limited = exp.select("*").from_(query.subquery("_audit")).limit(self.sample_rows)
                frame = adapter.fetchdf(limited)
            except Exception:
                logger.debug("sqlmesh-observability: sampling failed", exc_info=True)
                continue
            for record in frame.to_dict(orient="records"):
                samples.append(
                    {
                        "invocation_id": audit.invocation_id,
                        "model_name": audit.model_name,
                        "audit_name": audit.audit_name,
                        "interval_start": _ms_to_datetime(audit.interval_start),
                        "row_json": json.dumps(_plain(record), sort_keys=True, default=str),
                    }
                )
        return samples


def _plain(record: t.Mapping[str, t.Any]) -> t.Dict[str, t.Any]:
    """pandas uses NaN/NaT for missing values; store them as JSON null."""
    out: t.Dict[str, t.Any] = {}
    for key, value in record.items():
        try:
            import pandas as pd

            missing = bool(pd.isna(value))
        except (TypeError, ValueError):
            missing = False
        out[str(key)] = None if missing else value
    return out
