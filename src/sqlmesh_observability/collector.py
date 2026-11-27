"""Turns SQLMesh execution observations into plain per-invocation records.

The collector never touches the warehouse. Observer callbacks copy plain values and return; a
finished :class:`Invocation` is handed to the writer that ``install()`` configured, which writes it
to the warehouse (``writer.py``). Tests may also keep finished invocations in memory.

Invocation boundaries:

* ``arm`` is called from the notification target's ``*_start`` hook with the environment and plan
  id SQLMesh reports. A new ``arm`` replaces a stale slot (``Context.run`` sends no end hook when
  there is nothing to do) and hands off any invocation still open as ``unfinished``.
* ``begin`` is called once per observer resolution, which SQLMesh does once per outer plan or run.
  The first resolution under an armed slot mints the invocation; later ones under the same slot
  reuse it (``Context.run`` restarting after ``CircuitBreakerError``).
* ``end`` is called from ``*_end`` / ``*_failure`` and hands the invocation off.
"""

from __future__ import annotations

import contextvars
import dataclasses
import datetime
import json
import logging
import threading
import typing as t
import uuid

logger = logging.getLogger(__name__)

Status = t.Literal["success", "failure", "unfinished"]


@dataclasses.dataclass
class ModelRun:
    """One model interval batch, from a ``model`` observation."""

    invocation_id: str
    model_name: str
    snapshot_id: str
    version: str
    interval_start: t.Optional[int]
    interval_end: t.Optional[int]
    batch_index: t.Optional[int]
    execution_time: t.Optional[int]
    audit_only: bool
    environment: t.Optional[str]
    started_at: datetime.datetime
    physical_table: t.Optional[str] = None
    finished_at: t.Optional[datetime.datetime] = None
    duration_ms: t.Optional[int] = None
    status: t.Optional[str] = None
    error_type: t.Optional[str] = None
    error_message: t.Optional[str] = None
    # Plain model metadata for passive monitors .
    kind: t.Optional[str] = None
    columns: t.Tuple[t.Tuple[str, str], ...] = ()
    time_column: t.Optional[str] = None
    time_column_format: t.Optional[str] = None
    model_dialect: t.Optional[str] = None


@dataclasses.dataclass
class SchemaCapture:
    """Declared columns of one snapshot, from a ``physical`` create or schema migration."""

    invocation_id: str
    model_name: str
    snapshot_id: str
    version: str
    action: str
    # Identifiers of the snapshots this one descends from, oldest first.
    previous_snapshot_ids: t.Tuple[str, ...]
    columns: t.Tuple[t.Tuple[str, str], ...]
    recorded_at: datetime.datetime


@dataclasses.dataclass
class AuditRun:
    """One audit of one model run, from an ``audit`` observation nested in a ``model`` one."""

    invocation_id: str
    audit_name: str
    model_name: t.Optional[str]
    snapshot_id: t.Optional[str]
    version: t.Optional[str]
    physical_table: t.Optional[str]
    interval_start: t.Optional[int]
    interval_end: t.Optional[int]
    environment: t.Optional[str]
    finished_at: datetime.datetime
    status: str = "pass"
    violation_count: t.Optional[int] = None
    skipped: bool = False
    blocking: t.Optional[bool] = None
    args_json: t.Optional[str] = None
    query_sql: t.Optional[str] = None
    error_type: t.Optional[str] = None
    error_message: t.Optional[str] = None


@dataclasses.dataclass
class Invocation:
    """One outer SQLMesh plan application or scheduler run."""

    invocation_id: str
    command: t.Optional[str]
    environment: t.Optional[str]
    plan_id: t.Optional[str]
    started_at: datetime.datetime
    finished_at: t.Optional[datetime.datetime] = None
    status: t.Optional[Status] = None
    models: t.List[ModelRun] = dataclasses.field(default_factory=list)
    audits: t.List[AuditRun] = dataclasses.field(default_factory=list)
    schemas: t.List[SchemaCapture] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class _Slot:
    command: str
    environment: t.Optional[str]
    plan_id: t.Optional[str]
    invocation_id: t.Optional[str] = None


@dataclasses.dataclass
class _OpenModel:
    record: ModelRun
    token: t.Optional[contextvars.Token]


# The model run currently executing on this worker. SQLMesh calls the observer synchronously in the
# worker's context, so nested physical/audit observations see the value set at model start.
_current_model: contextvars.ContextVar[t.Optional[ModelRun]] = contextvars.ContextVar(
    "sqlmesh_observability_current_model", default=None
)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _error_fields(error: t.Optional[BaseException]) -> t.Tuple[t.Optional[str], t.Optional[str]]:
    if error is None:
        return None, None
    return type(error).__name__, str(error)


def _to_ms(value: t.Any) -> t.Optional[int]:
    if value is None:
        return None
    from sqlmesh.utils.date import to_timestamp

    return int(to_timestamp(value))


class Collector:
    """Process-wide collector shared by every wrapped console and notification target."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._slot: t.Optional[_Slot] = None
        self._open: t.Dict[str, Invocation] = {}
        self._open_models: t.Dict[int, _OpenModel] = {}
        self.finished: t.List[Invocation] = []
        self.record_finished = False  # test-only; a long-lived process must not grow this list
        self.writer: t.Optional[t.Any] = None  # sqlmesh_observability.writer.Writer
        self.dialect: t.Optional[str] = None  # dialect of the project's default gateway
        self.settings: t.Dict[str, t.Any] = {}

    # --- Boundaries (notification target and console wrap) --------------------------------------

    def arm(self, command: str, environment: t.Optional[str], plan_id: t.Optional[str]) -> None:
        with self._lock:
            stale = list(self._open)
            self._slot = _Slot(command=command, environment=environment, plan_id=plan_id)
        for invocation_id in stale:
            self._hand_off(invocation_id, "unfinished")

    def begin(self) -> str:
        with self._lock:
            slot = self._slot
            if slot is not None and slot.invocation_id in self._open:
                return slot.invocation_id  # type: ignore[return-value]
            stale = list(self._open)
            invocation = Invocation(
                invocation_id=uuid.uuid4().hex,
                command=slot.command if slot else None,
                environment=slot.environment if slot else None,
                plan_id=slot.plan_id if slot else None,
                started_at=_now(),
            )
            self._open[invocation.invocation_id] = invocation
            if slot is not None:
                slot.invocation_id = invocation.invocation_id
        for invocation_id in stale:
            self._hand_off(invocation_id, "unfinished")
        return invocation.invocation_id

    def end(self, status: Status) -> None:
        with self._lock:
            slot, self._slot = self._slot, None
        if slot is not None and slot.invocation_id is not None:
            self._hand_off(slot.invocation_id, status)

    def _hand_off(self, invocation_id: str, status: Status) -> None:
        with self._lock:
            invocation = self._open.pop(invocation_id, None)
        if invocation is None:
            return
        invocation.status = status
        invocation.finished_at = _now()
        if self.record_finished:
            self.finished.append(invocation)
        writer = self.writer
        if writer is not None:
            try:
                writer.submit_and_wait(invocation)
            except Exception:
                logger.debug("sqlmesh-observability writer hand-off failed", exc_info=True)

    # --- Observations -----------------------------------------------------------------------------

    def handle(
        self,
        invocation_id: str,
        phase: str,
        facts: t.Mapping[str, t.Any],
        error: t.Optional[BaseException],
    ) -> None:
        kind = facts.get("kind")
        if kind == "model":
            if phase == "start":
                self._model_start(invocation_id, facts)
            else:
                self._model_finish(facts, error)
        elif phase != "finish":
            return
        elif kind == "physical":
            physical_action = facts.get("action")
            if physical_action == "materialize":
                current = _current_model.get()
                if current is not None and current.invocation_id == invocation_id:
                    target = facts.get("target")
                    current.physical_table = None if target is None else str(target)
            if error is None and (
                physical_action in ("create", "schema_migration")
                or (physical_action == "materialize" and facts.get("creating"))
            ):
                self._capture_schema(invocation_id, str(physical_action), facts.get("snapshot"))
        elif kind == "audit":
            self._audit_finish(invocation_id, facts, error)
        elif kind == "stage":
            self._stage_finish(invocation_id, facts)

    def _invocation(self, invocation_id: str) -> t.Optional[Invocation]:
        with self._lock:
            return self._open.get(invocation_id)

    def _model_start(self, invocation_id: str, facts: t.Mapping[str, t.Any]) -> None:
        invocation = self._invocation(invocation_id)
        snapshot = facts.get("snapshot")
        interval = facts.get("interval") or (None, None)
        record = ModelRun(
            invocation_id=invocation_id,
            model_name=str(getattr(snapshot, "name", "")),
            snapshot_id=str(getattr(snapshot, "identifier", "")),
            version=str(getattr(snapshot, "version", "")),
            interval_start=_to_ms(interval[0]),
            interval_end=_to_ms(interval[1]),
            batch_index=facts.get("batch_index"),
            execution_time=_to_ms(facts.get("execution_time")),
            audit_only=bool(facts.get("audit_only", False)),
            environment=invocation.environment if invocation else None,
            started_at=_now(),
        )
        self._copy_model_metadata(record, _model_of(snapshot))
        token = _current_model.set(record)
        with self._lock:
            self._open_models[id(facts)] = _OpenModel(record=record, token=token)

    def _columns(self, model: t.Any) -> t.Tuple[t.Tuple[str, str], ...]:
        try:
            columns = model.columns_to_types
        except Exception:
            return ()
        if not columns:
            return ()
        return tuple(
            (str(name), kind.sql(dialect=self.dialect) if hasattr(kind, "sql") else str(kind))
            for name, kind in columns.items()
        )

    def _copy_model_metadata(self, record: ModelRun, model: t.Any) -> None:
        """Copy plain strings the writer needs for passive monitors; ``model`` is not kept."""
        if model is None:
            return
        try:
            record.kind = str(model.kind.name.value)
            record.columns = self._columns(model)
            record.model_dialect = str(model.dialect) if model.dialect is not None else None
            time_column = getattr(model, "time_column", None)
            if time_column is not None:
                record.time_column = str(time_column.column.name)
                record.time_column_format = (
                    None if time_column.format is None else str(time_column.format)
                )
        except Exception:
            logger.debug("sqlmesh-observability: could not copy model metadata", exc_info=True)

    def _capture_schema(self, invocation_id: str, action: str, snapshot: t.Any) -> None:
        invocation = self._invocation(invocation_id)
        model = _model_of(snapshot)
        if invocation is None or model is None:
            return
        try:
            if getattr(snapshot, "is_symbolic", False):
                return
            columns = self._columns(model)
            if not columns:
                return
            capture = SchemaCapture(
                invocation_id=invocation_id,
                model_name=str(snapshot.name),
                snapshot_id=str(snapshot.identifier),
                version=str(snapshot.version),
                action=action,
                previous_snapshot_ids=tuple(
                    str(v.fingerprint.to_identifier()) for v in snapshot.previous_versions
                ),
                columns=columns,
                recorded_at=_now(),
            )
        except Exception:
            logger.debug("sqlmesh-observability: could not capture schema", exc_info=True)
            return
        with self._lock:
            invocation.schemas[:] = [
                s for s in invocation.schemas if s.snapshot_id != capture.snapshot_id
            ]
            invocation.schemas.append(capture)

    def _model_finish(self, facts: t.Mapping[str, t.Any], error: t.Optional[BaseException]) -> None:
        with self._lock:
            opened = self._open_models.pop(id(facts), None)
        if opened is None:
            return
        record = opened.record
        if opened.token is not None:
            try:
                _current_model.reset(opened.token)
            except ValueError:  # token from another context; never leave this worker pointing at it
                _current_model.set(None)
        record.finished_at = _now()
        record.duration_ms = int((record.finished_at - record.started_at).total_seconds() * 1000)
        record.status = "failure" if error is not None else "success"
        record.error_type, record.error_message = _error_fields(error)
        invocation = self._invocation(record.invocation_id)
        if invocation is not None:
            with self._lock:
                invocation.models.append(record)

    def _audit_finish(
        self, invocation_id: str, facts: t.Mapping[str, t.Any], error: t.Optional[BaseException]
    ) -> None:
        invocation = self._invocation(invocation_id)
        if invocation is None:
            return
        model = _current_model.get()
        if model is not None and model.invocation_id != invocation_id:
            model = None
        error_type, error_message = _error_fields(error)
        result = facts.get("audit_result")
        if error is None and result is None:
            error_type, error_message = "MissingAuditResult", "audit finished without a result"
        record = AuditRun(
            invocation_id=invocation_id,
            audit_name=str(facts.get("audit_name", "")),
            model_name=model.model_name if model else None,
            snapshot_id=model.snapshot_id if model else None,
            version=model.version if model else None,
            physical_table=model.physical_table if model else None,
            interval_start=model.interval_start if model else None,
            interval_end=model.interval_end if model else None,
            environment=invocation.environment,
            finished_at=_now(),
            error_type=error_type,
            error_message=error_message,
        )
        if result is not None:
            self._copy_audit_result(record, result)
        record.status = _audit_status(record)
        with self._lock:
            invocation.audits.append(record)

    def _copy_audit_result(self, record: AuditRun, result: t.Any) -> None:
        """Copy plain values from a SQLMesh ``AuditResult``; the object itself is not kept."""
        count = getattr(result, "count", None)
        record.violation_count = None if count is None else int(count)
        record.skipped = bool(getattr(result, "skipped", False))
        blocking = getattr(result, "blocking", None)
        record.blocking = None if blocking is None else bool(blocking)
        record.args_json = _args_json(getattr(result, "audit_args", None) or {}, self.dialect)
        query = getattr(result, "query", None)
        if query is not None:
            try:
                record.query_sql = query.sql(dialect=self.dialect)
            except Exception:
                logger.debug("sqlmesh-observability: could not render audit SQL", exc_info=True)

    def _stage_finish(self, invocation_id: str, facts: t.Mapping[str, t.Any]) -> None:
        invocation = self._invocation(invocation_id)
        if invocation is None:
            return
        with self._lock:
            if invocation.command is None:
                invocation.command = "plan"
            if invocation.plan_id is None and facts.get("native_plan_id") is not None:
                invocation.plan_id = str(facts["native_plan_id"])


def _model_of(snapshot: t.Any) -> t.Any:
    """The snapshot's model, or None for a standalone audit or anything else that isn't a model.

    ``Snapshot.model`` raises ``SQLMeshError`` (not ``AttributeError``) for non-model snapshots,
    so ``getattr(snapshot, "model", None)`` would raise; ``model_or_none`` doesn't.
    """
    try:
        return getattr(snapshot, "model_or_none", None)
    except Exception:
        return None


def _audit_status(record: AuditRun) -> str:
    """Worst status of an audit's results: error, then skip, then fail, otherwise pass."""
    if record.error_type is not None:
        return "error"
    if record.skipped:
        return "skip"
    if record.violation_count:
        return "fail"
    return "pass"


def _args_json(args: t.Mapping[t.Any, t.Any], dialect: t.Optional[str]) -> str:
    def text(value: t.Any) -> str:
        sql = getattr(value, "sql", None)
        if callable(sql):
            try:
                return str(sql(dialect=dialect))
            except Exception:
                pass
        return str(value)

    return json.dumps({str(k): text(v) for k, v in args.items()}, sort_keys=True)


_COLLECTOR = Collector()


def get_collector() -> Collector:
    return _COLLECTOR


def _reset_for_tests() -> None:
    """Drop all collector state and keep finished invocations in memory. Test-only."""
    global _COLLECTOR
    previous = _COLLECTOR
    if previous.writer is not None:
        try:
            previous.writer.stop()
        except Exception:
            pass
    _COLLECTOR = Collector()
    _COLLECTOR.record_finished = True
