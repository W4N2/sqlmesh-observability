"""``sqlmesh-observe monitor``: one grouped message per owner for new prod issues.

It reads the same scoped data as the report, keeps failed or errored audits and anomalous metric
points that are newer than the environment's watermark and came from an invocation that targeted
the environment, and groups them by model owner from SQLMesh state. It sends each group through
the ``alert_target`` given to ``install()``, or prints it. The watermark in ``alert_watermarks``,
the only table the read side writes, moves only after every send returns.
"""

from __future__ import annotations

import datetime
import sys
import typing as t
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlglot import exp

if t.TYPE_CHECKING:
    from sqlmesh.core.engine_adapter import EngineAdapter

    from sqlmesh_observability.reader import AuditRecord, ReportData

WATERMARK_TABLE = "alert_watermarks"
WATERMARK_COLUMNS: t.Dict[str, exp.DataType] = {
    "environment": exp.DataType.build("TEXT"),
    "watermark_at": exp.DataType.build("TIMESTAMP"),
    "invocation_id": exp.DataType.build("TEXT"),
    "updated_at": exp.DataType.build("TIMESTAMP"),
}

# Severity order within a message: blocking failures and errors, then warnings, then anomalies.
_BLOCKING, _WARNING, _ANOMALY = 0, 1, 2

Mark = t.Tuple[datetime.datetime, str]  # (invocation finished_at, invocation_id)


@dataclass(frozen=True)
class Alert:
    """One new issue, as plain values: what the target receives in ``alerts=``."""

    kind: str  # "audit" or "anomaly"
    severity: int
    model_name: str
    owner: t.Optional[str]
    signal: str  # audit name with arguments, or metric with column
    interval_start: t.Optional[str]
    status: str  # "blocking failure", "non-blocking failure", "audit error", "anomaly"
    detail: str
    invocation_id: str

    def line(self) -> str:
        day = (self.interval_start or "-")[:10]
        name = self.model_name.replace('"', "")
        return f"- {name} · {self.signal} · {day} · {self.status}" + (
            f" · {self.detail}" if self.detail else ""
        )


@dataclass(frozen=True)
class Group:
    owner: t.Optional[str]
    alerts: t.Tuple[Alert, ...]

    @property
    def failing(self) -> bool:
        return any(a.severity == _BLOCKING for a in self.alerts)

    def message(self, environment: str) -> str:
        count = len(self.alerts)
        whom = f"for {self.owner}" if self.owner else "with no owner"
        noun = "issue" if count == 1 else "issues"
        head = f"sqlmesh-observe: {count} new {noun} in {environment} {whom}"
        return "\n".join([head, *(a.line() for a in self.alerts)])


def _rows(count: t.Optional[int]) -> str:
    if count is None:
        return ""
    return f"{count} row" if count == 1 else f"{count} rows"


def _audit_alert(record: "AuditRecord", owner: t.Optional[str]) -> Alert:
    from sqlmesh_observability.textfmt import args_text

    args = args_text(record.args_json)
    signal = f"{record.audit_name}({args})" if args else record.audit_name
    if record.status == "error":
        status, severity, detail = "audit error", _BLOCKING, record.error_message or ""
    elif record.blocking:
        status, severity, detail = "blocking failure", _BLOCKING, _rows(record.violation_count)
    else:
        status, severity, detail = "non-blocking failure", _WARNING, _rows(record.violation_count)
    return Alert(
        kind="audit",
        severity=severity,
        model_name=record.model_name,
        owner=owner,
        signal=signal,
        interval_start=record.interval_start,
        status=status,
        detail=detail,
        invocation_id=record.invocation_id,
    )


def candidates(data: "ReportData") -> t.List[Alert]:
    """Every failed or errored audit and every anomalous point in scope, old or new."""
    from sqlmesh_observability.textfmt import expected_range as _expected
    from sqlmesh_observability.textfmt import number as _num

    owners = {m.model_name: m.owner for m in data.models}
    found = [_audit_alert(r, owners.get(r.model_name)) for r in data.incidents]
    for model in data.models:
        for series in model.metrics:
            signal = series.metric_name + (f"({series.column_name})" if series.column_name else "")
            for point in series.points:
                if not point.anomaly or not point.invocation_id:
                    continue
                expected = _expected(point.mean, point.stddev, series.threshold)
                found.append(
                    Alert(
                        kind="anomaly",
                        severity=_ANOMALY,
                        model_name=model.model_name,
                        owner=model.owner,
                        signal=f"{signal} anomaly",
                        interval_start=point.interval_start,
                        status="anomaly",
                        detail=f"{_num(point.value)} (expected {expected})",
                        invocation_id=point.invocation_id,
                    )
                )
    return found


def group(alerts: t.Iterable[Alert]) -> t.List[Group]:
    """One group per owner, ordered by owner with the owner-less group last."""
    by_owner: t.Dict[t.Optional[str], t.List[Alert]] = {}
    for alert in alerts:
        by_owner.setdefault(alert.owner, []).append(alert)
    ordered = sorted(by_owner, key=lambda owner: (owner is None, owner or ""))
    return [
        Group(
            owner=owner,
            alerts=tuple(
                sorted(
                    by_owner[owner],
                    key=lambda a: (
                        a.severity,
                        a.model_name,
                        a.signal,
                        a.interval_start or "",
                        a.invocation_id,
                    ),
                )
            ),
        )
        for owner in ordered
    ]


def _table(schema: str) -> exp.Table:
    return exp.to_table(f"{schema}.{WATERMARK_TABLE}")


def _invocation_marks(
    adapter: "EngineAdapter", schema: str, invocation_ids: t.Collection[str]
) -> t.Dict[str, t.Tuple[t.Optional[str], Mark]]:
    """``{invocation_id: (environment, (finished_at or started_at, invocation_id))}``."""
    from sqlmesh_observability.reader import _IN_CHUNK, _naive

    marks: t.Dict[str, t.Tuple[t.Optional[str], Mark]] = {}
    table = exp.to_table(f"{schema}.invocations")
    ids = sorted(set(invocation_ids))
    if not ids or not adapter.table_exists(table):
        return marks
    for start in range(0, len(ids), _IN_CHUNK):
        chunk = ids[start : start + _IN_CHUNK]
        query = (
            exp.select("invocation_id", "environment", "finished_at", "started_at")
            .from_(table)
            .where(exp.column("invocation_id").isin(*(exp.Literal.string(i) for i in chunk)))
        )
        for invocation_id, environment, finished_at, started_at in adapter.fetchall(query):
            when = _naive(finished_at) or _naive(started_at) or datetime.datetime.min
            marks[str(invocation_id)] = (
                None if environment is None else str(environment),
                (when, str(invocation_id)),
            )
    return marks


def read_watermark(adapter: "EngineAdapter", schema: str, environment: str) -> t.Optional[Mark]:
    from sqlmesh_observability.reader import _naive

    table = _table(schema)
    if not adapter.table_exists(table):
        return None
    row = adapter.fetchone(
        exp.select("watermark_at", "invocation_id")
        .from_(table)
        .where(exp.column("environment").eq(exp.Literal.string(environment)))
        .order_by(exp.column("watermark_at").desc(), exp.column("invocation_id").desc())
        .limit(1)
    )
    if row is None or row[0] is None:
        return None
    return (_naive(row[0]) or datetime.datetime.min, str(row[1] or ""))


def write_watermark(adapter: "EngineAdapter", schema: str, environment: str, mark: Mark) -> None:
    """Replace the environment's watermark row. The read side's only write."""
    from sqlmesh_observability.writer import _literal

    table = _table(schema)
    adapter.create_table(table, WATERMARK_COLUMNS, exists=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    row = {
        "environment": environment,
        "watermark_at": mark[0],
        "invocation_id": mark[1],
        "updated_at": now,
    }
    values = exp.values(
        [tuple(_literal(row[c], kind) for c, kind in WATERMARK_COLUMNS.items())],
        alias="_rows",
        columns=list(WATERMARK_COLUMNS),
    )
    with adapter.transaction():
        adapter.delete_from(table, exp.column("environment").eq(exp.Literal.string(environment)))
        adapter.insert_append(
            table,
            exp.select(*(exp.column(c) for c in WATERMARK_COLUMNS)).from_(values),
            target_columns_to_types=WATERMARK_COLUMNS,
        )


def run_monitor(
    path: t.Union[str, Path] = ".",
    gateway: t.Optional[str] = None,
    schema: t.Optional[str] = None,
    environment: str = "prod",
) -> int:
    """Send (or print) one message per owner for new issues; return the exit code."""
    from sqlmesh.core.notification_target import NotificationStatus

    from sqlmesh_observability.reader import (
        _load_project_config,
        _resolve_gateway_and_schema,
        load_report_data_for,
    )

    config, settings = _load_project_config(path)
    target = settings.get("alert_target")
    data = load_report_data_for(config, settings, gateway, schema, environment)
    resolved_gateway, resolved_schema = _resolve_gateway_and_schema(
        config, settings, gateway, schema
    )

    adapter = config.get_connection(resolved_gateway).create_engine_adapter()
    try:
        found = candidates(data)
        marks = _invocation_marks(adapter, resolved_schema, {a.invocation_id for a in found})
        watermark = read_watermark(adapter, resolved_schema, environment)
        new = [
            a
            for a in found
            if a.invocation_id in marks
            and marks[a.invocation_id][0] == environment
            and (watermark is None or marks[a.invocation_id][1] > watermark)
        ]
        if not new:
            print(f"sqlmesh-observe: no new issues in {environment}")
            return 0

        for item in group(new):
            message = item.message(environment)
            if target is None:
                print(message + "\n")
                continue
            status = NotificationStatus.FAILURE if item.failing else NotificationStatus.WARNING
            try:
                target.send(
                    status, message, owner=item.owner, alerts=[asdict(a) for a in item.alerts]
                )
            except Exception as error:
                print(
                    f"sqlmesh-observe: sending the alert for {item.owner or 'models with no owner'}"
                    f" failed, so the watermark was not moved: {error}",
                    file=sys.stderr,
                )
                return 1

        write_watermark(
            adapter,
            resolved_schema,
            environment,
            max(marks[a.invocation_id][1] for a in new),
        )
        return 0
    finally:
        adapter.close()
