"""The report's data: one compact, versioned JSON payload that the page draws in the browser.

Status, scope, and anomaly flags are decided here in Python. The browser only displays, filters,
and sorts. Repeated values go into tables and per-interval rows are short arrays, so a project
with 1,500 models stays a few megabytes.

Schema version 1::

    schema_version, environment, schema, gateway, generated_at, state_available,
    state_gateway, history, monitors
    dates     ["2024-03-31", ...]          every interval start referenced below, sorted
    owners    ["team-00", ...]             owner names; models refer to them by index
    audits    [[name, args_text], ...]     audit uses (name plus arguments)
    metrics   ["row_count", ...]           metric names
    models    [{n: full name, o: owner index or -1, st: "pass" | "warn" | "fail",
                c: [[column, type], ...],
                h: [dates, health codes]                  oldest first
                r: [dates, durations_ms]                  oldest first
                a: [[date, use, status, violations, blocking, incident], ...]  newest first
                m: [[metric, column, threshold, dates, values, means, sds, flags], ...]
                x: [[column, change, old_type, new_type, recorded_at, to_version], ...]}]

    ``dates`` inside ``h``, ``r`` and ``m`` are delta-coded: the first entry is an index into
    the top-level ``dates`` and each later one is the step from the previous index. A run of
    consecutive dates is shortened to ``[-count, first]``. Means and
    standard deviations keep four significant digits, values six.
    incidents [{model, use, interval, invocation, snapshot, version, environment, status,
                blocking, violations, sql, error, started_at, samples: [{...}]}]
    issues    [{kind, model, signal, interval, value, lo, hi, incident, metric, column}]
    trend     [[date, executions, failures, warnings_and_anomalies], ...]
    summary   {models: {pass, warn, fail}, audits: {pass, warn, fail, error},
               monitored: {monitored, unmonitored}}
    signals   {audits, volume, freshness, schema, columns: {pass, warn, fail} | null}
    runs      [{id, command, environment, status, started_at, finished_at, duration_ms,
                model_runs, failed_runs, audits, failing_audits, blocking_failures}]
    lineage   {extra: [model names], kinds: [kind names],
               kind: [kind index or -1], parents: [[node indexes]], impact: [count]}

    Lineage nodes ``0 .. len(models) - 1`` are ``models``; node ``len(models) + i`` is
    ``extra[i]``, a model in the environment with no observability rows (an external source,
    say). ``impact`` is the number of distinct models downstream of each node. Each issue
    carries its model's ``impact``.

Version 2 added ``lineage`` and ``issues[].impact``.

Codes: ``health`` 0 pass, 1 warn, 2 fail, 3 error, 4 skip. Audit ``status`` 0 pass, 1 fail,
2 error, 3 skip. Point ``flags`` bit 1 anomaly, bit 2 error, bit 4 still training. Indexes are
-1 when absent.
"""

from __future__ import annotations

import json
import logging
import typing as t

from sqlmesh_observability.reader import audit_key
from sqlmesh_observability.textfmt import args_text

if t.TYPE_CHECKING:
    from sqlmesh_observability.reader import (
        AuditRecord,
        MetricSeries,
        ModelReport,
        ReportData,
    )

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
DEFAULT_HISTORY = 30

_HEALTH = {"pass": 0, "warn": 1, "fail": 2, "error": 3, "skip": 4}
_AUDIT_STATUS = {"pass": 0, "fail": 1, "error": 2, "skip": 3}
_VOLUME = "row_count"
_FRESHNESS = "freshness_lag_seconds"


def _num(value: t.Optional[float], digits: int = 6) -> t.Optional[float]:
    """``digits`` significant digits; integers stay integers."""
    if value is None:
        return None
    rounded = float(f"{value:.{digits}g}")
    return int(rounded) if rounded.is_integer() and abs(rounded) < 1e15 else rounded


def _day(value: str) -> str:
    return value[:10] if value.endswith(" 00:00:00") else value


def model_status(model: "ModelReport") -> str:
    """``fail`` (blocking failure or error), ``warn`` (non-blocking failure or a latest-point
    anomaly), or ``pass``."""
    if model.failing_blocking_audits:
        return "fail"
    if model.warning_audits or any(s.points and s.points[-1].anomaly for s in model.metrics):
        return "warn"
    return "pass"


def _latest_per_interval(audits: t.Sequence["AuditRecord"]) -> t.List["AuditRecord"]:
    """The latest run of each (audit use, interval), newest interval first."""
    latest: t.Dict[t.Tuple[t.Any, ...], "AuditRecord"] = {}
    for audit in audits:
        key = audit_key(audit)
        seen = latest.get(key)
        if seen is None or (audit.started_at or "", audit.invocation_id) > (
            seen.started_at or "",
            seen.invocation_id,
        ):
            latest[key] = audit
    return sorted(
        latest.values(),
        key=lambda a: (a.interval_start or "", a.audit_name, a.args_json or ""),
        reverse=True,
    )


def _incident_key(audit: "AuditRecord") -> t.Tuple[t.Any, ...]:
    return (audit.invocation_id, *audit_key(audit))


def _samples(raw: t.Sequence[str]) -> t.List[t.Any]:
    rows = []
    for row in raw:
        try:
            rows.append(json.loads(row))
        except ValueError:
            rows.append(row)
    return rows


class _Tables:
    """Interning tables for repeated strings."""

    def __init__(self) -> None:
        self.dates: t.Dict[str, int] = {}
        self.owners: t.Dict[str, int] = {}
        self.uses: t.Dict[t.Tuple[str, str], int] = {}
        self.metrics: t.Dict[str, int] = {}

    @staticmethod
    def _index(table: t.Dict[t.Any, int], value: t.Any) -> int:
        if value not in table:
            table[value] = len(table)
        return table[value]

    def date(self, value: str) -> int:
        return self._index(self.dates, _day(value))

    def owner(self, value: t.Optional[str]) -> int:
        return -1 if value is None else self._index(self.owners, value)

    def use(self, audit: "AuditRecord") -> int:
        return self._index(self.uses, (audit.audit_name, args_text(audit.args_json)))

    def metric(self, value: str) -> int:
        return self._index(self.metrics, value)


def _split(statuses: t.Iterable[str]) -> t.Dict[str, int]:
    counts = {"pass": 0, "warn": 0, "fail": 0}
    for status in statuses:
        counts[status] += 1
    return counts


def _series_status(series: "MetricSeries") -> str:
    point = series.points[-1]
    if point.error_message:
        return "fail"
    return "warn" if point.anomaly else "pass"


def _signals(
    data: "ReportData", latest: t.Mapping[str, t.List["AuditRecord"]]
) -> t.Dict[str, t.Any]:
    def audit_status(audit: "AuditRecord") -> str:
        if audit.status == "error" or (audit.status == "fail" and audit.blocking):
            return "fail"
        return "warn" if audit.status == "fail" else "pass"

    newest: t.List["AuditRecord"] = []
    for rows in latest.values():
        seen: t.Set[t.Tuple[str, str]] = set()
        for audit in rows:  # newest interval first: the first row per use is its latest
            use = (audit.audit_name, audit.args_json or "")
            if use not in seen:
                seen.add(use)
                newest.append(audit)

    def metric_signal(match: t.Callable[[str], bool], monitor: str) -> t.Optional[t.Dict[str, int]]:
        series = [s for m in data.models for s in m.metrics if match(s.metric_name) and s.points]
        if not series and monitor not in data.monitors:
            return None
        return _split(_series_status(s) for s in series)

    schema: t.Optional[t.Dict[str, int]] = None
    if "schema" in data.monitors or any(m.schema_changes for m in data.models):
        statuses = []
        for model in data.models:
            kinds = {c.change_type for c in model.schema_changes}
            statuses.append("pass" if not kinds else ("warn" if kinds <= {"added"} else "fail"))
        schema = _split(statuses)

    return {
        "audits": _split(audit_status(a) for a in newest),
        "volume": metric_signal(lambda name: name == _VOLUME, "volume"),
        "freshness": metric_signal(lambda name: name == _FRESHNESS, "freshness"),
        "schema": schema,
        "columns": metric_signal(lambda name: name not in (_VOLUME, _FRESHNESS), "columns"),
    }


def build_payload(data: "ReportData", history: int = DEFAULT_HISTORY) -> t.Dict[str, t.Any]:
    """Everything the page shows, with each per-interval history cut to its last ``history``
    intervals. Anomaly flags come from the reader, which scored the full history."""
    tables = _Tables()
    model_index = {m.model_name: i for i, m in enumerate(data.models)}

    latest = {m.model_name: _latest_per_interval(m.audits) for m in data.models}
    open_audits = [a for m in data.failing_models for a in m.failing_blocking_audits] + [
        a for m in data.warning_models for a in m.warning_audits
    ]
    open_keys = {_incident_key(a) for a in open_audits}

    # Retained audit rows: the latest run of each use for the last ``history`` intervals.
    kept: t.Dict[str, t.List["AuditRecord"]] = {}
    for name, rows in latest.items():
        starts = sorted({a.interval_start or "" for a in rows}, reverse=True)[:history]
        window = set(starts)
        kept[name] = [a for a in rows if (a.interval_start or "") in window]

    kept_keys = {_incident_key(a) for rows in kept.values() for a in rows}
    incidents: t.List[t.Dict[str, t.Any]] = []
    incident_index: t.Dict[t.Tuple[t.Any, ...], int] = {}
    for audit in data.incidents:
        key = _incident_key(audit)
        if key in incident_index or (key not in kept_keys and key not in open_keys):
            continue
        incident_index[key] = len(incidents)
        incidents.append(
            {
                "model": model_index.get(audit.model_name, -1),
                "use": tables.use(audit),
                "interval": _day(audit.interval_start) if audit.interval_start else None,
                "invocation": audit.invocation_id,
                "snapshot": audit.snapshot_id,
                "version": audit.version,
                "environment": audit.environment,
                "status": audit.status,
                "blocking": audit.blocking,
                "violations": audit.violation_count,
                "sql": audit.query_sql,
                "error": audit.error_message,
                "started_at": audit.started_at,
                "samples": _samples(audit.samples),
            }
        )

    models: t.List[t.Dict[str, t.Any]] = []
    for model in data.models:
        audit_rows: t.List[t.List[t.Any]] = []
        for audit in kept[model.model_name]:
            audit_rows.append(
                [
                    tables.date(audit.interval_start or ""),
                    tables.use(audit),
                    _AUDIT_STATUS.get(audit.status, 3),
                    audit.violation_count,
                    1 if audit.blocking else 0,
                    incident_index.get(_incident_key(audit), -1),
                ]
            )
        run_by_interval: t.Dict[str, int] = {}
        for run in model.runs:  # oldest first: the latest run of an interval wins
            if run.interval_start is not None and run.duration_ms is not None:
                run_by_interval[run.interval_start] = run.duration_ms
        series = []
        for s in model.metrics:
            points = s.points[-history:]
            series.append(
                [
                    tables.metric(s.metric_name),
                    s.column_name,
                    s.threshold,
                    [tables.date(p.interval_start) for p in points],
                    [_num(p.value) for p in points],
                    [_num(p.mean, 4) for p in points],
                    [_num(p.stddev, 4) for p in points],
                    [
                        (1 if p.anomaly else 0)
                        | (2 if p.error_message else 0)
                        | (4 if p.mean is None else 0)
                        for p in points
                    ],
                ]
            )
        health = model.health[-history:]
        durations = sorted(run_by_interval.items())[-history:]
        models.append(
            {
                "n": model.model_name,
                "o": tables.owner(model.owner),
                "st": model_status(model),
                "c": [list(c) for c in model.columns],
                "h": [
                    [tables.date(h.interval_start) for h in health],
                    [_HEALTH.get(h.status, 4) for h in health],
                ],
                "r": [[tables.date(k) for k, _ in durations], [v for _, v in durations]],
                "a": audit_rows,
                "m": series,
                "x": [
                    [
                        c.column_name,
                        c.change_type,
                        c.old_type,
                        c.new_type,
                        c.recorded_at,
                        c.to_version,
                    ]
                    for c in model.schema_changes
                ],
            }
        )

    issues: t.List[t.Dict[str, t.Any]] = []
    for m in data.failing_models:
        for audit in m.failing_blocking_audits:
            issues.append(
                _audit_issue(
                    audit,
                    "error" if audit.status == "error" else "blocking",
                    model_index,
                    incident_index,
                    tables,
                )
            )
    for anomaly in data.anomalies:
        point = anomaly.point
        threshold = (
            next(
                (
                    s.threshold
                    for s in data.models[model_index[anomaly.model_name]].metrics
                    if s.metric_name == anomaly.metric_name and s.column_name == anomaly.column_name
                ),
                3.0,
            )
            if anomaly.model_name in model_index
            else 3.0
        )
        lo = hi = None
        if point.mean is not None and point.stddev is not None:
            lo, hi = point.mean - threshold * point.stddev, point.mean + threshold * point.stddev
        issues.append(
            {
                "kind": "anomaly",
                "model": model_index.get(anomaly.model_name, -1),
                "signal": anomaly.metric_name,
                "column": anomaly.column_name,
                "interval": _day(point.interval_start),
                "value": _num(point.value),
                "lo": _num(lo),
                "hi": _num(hi),
                "incident": -1,
            }
        )
    for m in data.warning_models:
        for audit in m.warning_audits:
            issues.append(_audit_issue(audit, "warning", model_index, incident_index, tables))

    # Daily trend over the retained window: one row per interval day with audit results or an
    # anomaly. Days with no data get no row, so the chart draws no false zeros.
    trend: t.Dict[str, t.List[int]] = {}
    for rows in kept.values():
        for audit in rows:
            day = _day(audit.interval_start or "")
            counts = trend.setdefault(day, [0, 0, 0])
            counts[0] += 1
            if audit.status == "error" or (audit.status == "fail" and audit.blocking):
                counts[1] += 1
            elif audit.status == "fail":
                counts[2] += 1
    for model in data.models:
        for s in model.metrics:
            for p in s.points[-history:]:
                if p.anomaly:
                    trend.setdefault(_day(p.interval_start), [0, 0, 0])[2] += 1

    audit_summary = {"pass": 0, "warn": 0, "fail": 0, "error": 0}
    for rows in latest.values():
        seen: t.Set[t.Tuple[str, str]] = set()
        for audit in rows:
            use = (audit.audit_name, audit.args_json or "")
            if use in seen:
                continue
            seen.add(use)
            if audit.status == "error":
                audit_summary["error"] += 1
            elif audit.status == "fail":
                audit_summary["fail" if audit.blocking else "warn"] += 1
            elif audit.status == "pass":
                audit_summary["pass"] += 1
    monitored = sum(1 for m in data.models if m.metrics)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "environment": data.environment,
        "schema": data.schema,
        "gateway": data.gateway,
        "generated_at": data.generated_at,
        "state_available": data.state_available,
        "state_gateway": data.state_gateway,
        "history": history,
        "monitors": list(data.monitors),
        "models": models,
        "incidents": incidents,
        "issues": issues,
        "trend": [[tables.date(day), *counts] for day, counts in sorted(trend.items())],
        "summary": {
            "models": _split(m["st"] for m in models),
            "audits": audit_summary,
            "monitored": {"monitored": monitored, "unmonitored": len(models) - monitored},
        },
        "signals": _signals(data, latest),
        "runs": [
            {
                "id": inv.invocation_id,
                "command": inv.command,
                "environment": inv.environment,
                "status": inv.status,
                "started_at": inv.started_at,
                "finished_at": inv.finished_at,
                "duration_ms": inv.duration_ms,
                "model_runs": inv.model_runs,
                "failed_runs": inv.failed_runs,
                "audits": inv.audits,
                "failing_audits": inv.failing_audits,
                "blocking_failures": inv.blocking_failures,
            }
            for inv in data.invocations
        ],
    }
    lineage = _lineage(data, model_index)
    payload["lineage"] = lineage
    for issue in issues:
        issue["impact"] = lineage["impact"][issue["model"]] if issue["model"] >= 0 else 0
    # Tables last: everything above interned into them.
    payload["dates"] = list(tables.dates)
    payload["owners"] = list(tables.owners)
    payload["audits"] = [list(use) for use in tables.uses]
    payload["metrics"] = list(tables.metrics)
    return _sort_dates(payload)


def _lineage(data: "ReportData", model_index: t.Mapping[str, int]) -> t.Dict[str, t.Any]:
    """Every model in the environment as one node table, with parents and downstream impact."""
    index = dict(model_index)
    extra = [n.model_name for n in data.lineage if n.model_name not in index]
    for name in extra:
        index[name] = len(index)
    size = len(index)
    kinds: t.Dict[str, int] = {}
    kind = [-1] * size
    parents: t.List[t.List[int]] = [[] for _ in range(size)]
    children: t.List[t.List[int]] = [[] for _ in range(size)]
    for node in data.lineage:
        i = index[node.model_name]
        if node.kind is not None:
            kind[i] = kinds.setdefault(node.kind, len(kinds))
        for parent in node.parents:
            if parent in index:
                parents[i].append(index[parent])
                children[index[parent]].append(i)

    from sqlmesh.utils.dag import DAG

    impact = [0] * size
    try:
        dag: "DAG[int]" = DAG({i: set(p) for i, p in enumerate(parents)})
        for i in range(size):
            if children[i]:
                impact[i] = len(dag.downstream(i))
    except Exception:  # SQLMesh's DAG refuses a cycle; state should never hold one
        logger.debug("sqlmesh-observability: lineage has a cycle; impact counts off", exc_info=True)
    return {
        "extra": extra,
        "kinds": list(kinds),
        "kind": kind,
        "parents": [sorted(p) for p in parents],
        "impact": impact,
    }


def _audit_issue(
    audit: "AuditRecord",
    kind: str,
    model_index: t.Mapping[str, int],
    incident_index: t.Mapping[t.Tuple[t.Any, ...], int],
    tables: _Tables,
) -> t.Dict[str, t.Any]:
    return {
        "kind": kind,
        "model": model_index.get(audit.model_name, -1),
        "signal": audit.audit_name,
        "use": tables.use(audit),
        "interval": _day(audit.interval_start) if audit.interval_start else None,
        "value": audit.violation_count,
        "lo": 0,
        "hi": 0,
        "incident": incident_index.get(_incident_key(audit), -1),
    }


def _sort_dates(payload: t.Dict[str, t.Any]) -> t.Dict[str, t.Any]:
    """Renumber ``dates`` into sorted order so the browser can compare indexes as dates."""
    dates: t.List[str] = payload["dates"]
    order = sorted(range(len(dates)), key=lambda i: dates[i])
    remap = {old: new for new, old in enumerate(order)}
    payload["dates"] = [dates[i] for i in order]
    for model in payload["models"]:
        for row in model["a"]:
            row[0] = remap[row[0]]
        for column in (model["h"], model["r"], *model["m"]):
            index = 0 if column is model["h"] or column is model["r"] else 3
            column[index] = _deltas([remap[d] for d in column[index]])
    for row in payload["trend"]:
        row[0] = remap[row[0]]
    payload["trend"].sort(key=lambda row: row[0])
    return payload


def _deltas(indexes: t.List[int]) -> t.List[int]:
    if len(indexes) > 2 and indexes == list(range(indexes[0], indexes[0] + len(indexes))):
        return [-len(indexes), indexes[0]]
    return [d - (indexes[i - 1] if i else 0) for i, d in enumerate(indexes)]


def undelta(deltas: t.Sequence[int]) -> t.List[int]:
    """Decode a delta-coded date list back into indexes (the browser does the same)."""
    if len(deltas) == 2 and deltas[0] < 0:
        return list(range(deltas[1], deltas[1] - deltas[0]))
    out: t.List[int] = []
    for d in deltas:
        out.append(d + (out[-1] if out else 0))
    return out


def encode(payload: t.Mapping[str, t.Any]) -> str:
    """JSON that is safe inside ``<script type="application/json">``: nothing in it can close
    the element or start markup."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return (
        text.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )
