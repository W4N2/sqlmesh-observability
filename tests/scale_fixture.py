"""A seeded, synthetic ``ReportData`` for N models, built through the reader's own assembly.

Running 1,500 models through SQLMesh would make the suite slow, so the size-budget, payload, and
demo checks use this instead. Records go through ``reader._build_report_data`` so status, scope,
health, and anomaly lists are derived exactly as for a real project.

Shape, with ``n`` models and ``days`` daily intervals ending on ``END``:

* schemas ``demo.mart_00`` .. one per 100 models; owners ``team-00`` .. ``team-19``;
* every model: a passing blocking ``not_null(columns := (id))`` each day, a ``row_count`` series,
  and one run per interval;
* ``failing`` models: a blocking ``not_null(columns := (payment_id))`` that fails on the last day;
* ``warning`` models: a non-blocking ``not_null_non_blocking`` that fails on the last day;
* ``anomalous`` models: a ``row_count`` spike on the last day;
* every third model: a ``freshness_lag_seconds`` series; every tenth: ``null_rate`` on ``amount``;
* every 250th model: an added column.

Lineage: 20 external sources ``demo.raw.source_00`` .. feed models 0-299 (one each); every later
model reads one to three of the 200 models before it (models 300-399 also read model ``m - 300``),
and ``model_0300`` feeds models 1000-1399, a 400-child
fan-out. The failing models are early, so their downstream impact is wide.
"""

from __future__ import annotations

import datetime
import json
import math
import random
import typing as t

if t.TYPE_CHECKING:
    from sqlmesh_observability.reader import ReportData

END = datetime.datetime(2024, 3, 31)


def build_scale_data(
    n: int = 1500,
    days: int = 30,
    failing: int = 6,
    warning: int = 14,
    anomalous: int = 18,
    seed: int = 7,
    environment: str = "prod",
) -> "ReportData":
    from sqlmesh_observability.monitors import ScoreSettings, score_series
    from sqlmesh_observability.reader import (
        AuditRecord,
        InvocationRecord,
        LineageNode,
        MetricPoint,
        MetricSeries,
        RunRecord,
        SchemaChange,
        _build_report_data,
        _StateSnapshotInfo,
    )

    rng = random.Random(seed)
    dates = [END - datetime.timedelta(days=days - 1 - i) for i in range(days)]
    stamps = [d.strftime("%Y-%m-%d %H:%M:%S") for d in dates]
    inv_ids = [f"inv-{i:04d}" for i in range(days)]
    settings = ScoreSettings()

    records: t.List[AuditRecord] = []
    runs: t.Dict[str, t.List[RunRecord]] = {}
    metrics: t.Dict[str, t.Tuple[MetricSeries, ...]] = {}
    schema_changes: t.Dict[str, t.Tuple[SchemaChange, ...]] = {}
    owners: t.Dict[str, t.Optional[str]] = {}
    columns: t.Dict[str, t.Tuple[t.Tuple[str, str], ...]] = {}
    versions = set()

    def series(name: str, column: str, values: t.List[float]) -> MetricSeries:
        scored = score_series(list(zip(dates, values)), settings)
        return MetricSeries(
            metric_name=name,
            column_name=column,
            points=tuple(
                MetricPoint(
                    interval_start=stamps[i],
                    value=p.value,
                    mean=p.mean,
                    stddev=p.stddev,
                    training_points=p.training_points,
                    anomaly=p.anomaly,
                    invocation_id=inv_ids[i],
                )
                for i, p in enumerate(scored)
            ),
            threshold=settings.threshold,
        )

    def audit(model: str, i: int, name: str, args: str, status: str, blocking: bool, count: int):
        return AuditRecord(
            invocation_id=inv_ids[i],
            model_name=model,
            snapshot_id=f"snap-{model}",
            version="v1",
            physical_table=f"sqlmesh__{model}",
            environment=environment,
            audit_name=name,
            args_json=json.dumps({"columns": args}),
            interval_start=stamps[i],
            interval_end=stamps[i],
            status=status,
            violation_count=count,
            blocking=blocking,
            query_sql=f"SELECT * FROM {model} WHERE {args.strip('()')} IS NULL",
            error_message=None,
            started_at=stamps[i],
            samples=(json.dumps({"id": 1, "payment_id": None}),) if status == "fail" else (),
        )

    for m in range(n):
        name = f"demo.mart_{m // 100:02d}.model_{m:04d}"
        versions.add((name, "v1"))
        owners[name] = f"team-{m % 20:02d}"
        columns[name] = (("ds", "DATE"), ("payment_id", "INT"), ("amount", "DOUBLE"))
        last = days - 1
        for i in range(days):
            records.append(audit(name, i, "not_null", "(id)", "pass", True, 0))
        if m < failing:
            records.append(audit(name, last, "not_null", "(payment_id)", "fail", True, 4 + m))
        elif m < failing + warning:
            records.append(
                audit(name, last, "not_null_non_blocking", "(status)", "fail", False, 120)
            )
        runs[name] = [
            RunRecord(
                invocation_id=inv_ids[i],
                snapshot_id=f"snap-{name}",
                version="v1",
                interval_start=stamps[i],
                interval_end=stamps[i],
                status="success",
                duration_ms=int(40 + rng.random() * 30),
                started_at=stamps[i],
                environment=environment,
                audit_only=False,
                error_message=None,
            )
            for i in range(days)
        ]
        base = 1000 + 10 * (m % 50)
        volume = [base + 5 * i + 3 * math.sin(i + m) for i in range(days)]
        if failing + warning <= m < failing + warning + anomalous:
            volume[-1] = base * 3
        model_series = [series("row_count", "", volume)]
        if m % 3 == 0:
            lag = [3600 + 120 * math.sin(i * 0.9 + m) for i in range(days)]
            model_series.append(series("freshness_lag_seconds", "", lag))
        if m % 10 == 0:
            rate = [0.01 + 0.002 * math.sin(i * 0.9 + m) for i in range(days)]
            model_series.append(series("null_rate", "amount", rate))
        metrics[name] = tuple(model_series)
        if m % 250 == 0:
            schema_changes[name] = (
                SchemaChange(
                    column_name="amount",
                    change_type="added",
                    old_type=None,
                    new_type="DOUBLE",
                    from_version="v0",
                    to_version="v1",
                    from_snapshot_id=None,
                    to_snapshot_id=f"snap-{name}",
                    recorded_at=stamps[-1],
                ),
            )

    invocations = [
        InvocationRecord(
            invocation_id=inv_ids[i],
            command="plan" if i == days - 1 else "run",
            environment=environment,
            status="failure" if i == days - 1 and failing else "success",
            started_at=stamps[i],
            finished_at=stamps[i],
            duration_ms=int(60_000 + rng.random() * 30_000),
        )
        for i in reversed(range(days))
    ][:50]

    names = [f"demo.mart_{m // 100:02d}.model_{m:04d}" for m in range(n)]
    lineage_rng = random.Random(seed + 1)
    nodes = [
        LineageNode(model_name=f"demo.raw.source_{i:02d}", kind="EXTERNAL", parents=())
        for i in range(20)
    ]
    for m, name in enumerate(names):
        if m < min(n, 300):
            parents: t.Tuple[str, ...] = (f"demo.raw.source_{m % 20:02d}",)
        elif 1000 <= m < 1400 and n > 300:
            parents = (names[300],)
        else:
            picks = set(lineage_rng.sample(range(max(0, m - 200), m), lineage_rng.randint(1, 3)))
            if m < 400:
                picks.add(m - 300)  # models 0-99, the failing ones among them, feed onward
            parents = tuple(sorted(names[p] for p in picks))
        kind = "INCREMENTAL_BY_TIME_RANGE" if m % 4 else "FULL"
        nodes.append(LineageNode(model_name=name, kind=kind, parents=parents))
    lineage = tuple(sorted(nodes, key=lambda node: node.model_name))

    state = _StateSnapshotInfo(
        active_versions=frozenset(versions), owners=owners, columns=columns, gateway="warehouse"
    )
    data = _build_report_data(
        records,
        state,
        environment,
        "observability",
        "warehouse",
        metrics=metrics,
        schema_changes=schema_changes,
        runs=runs,
        invocations=invocations,
        monitors=("volume", "freshness", "schema", "columns"),
        lineage=lineage,
    )
    from dataclasses import replace

    return replace(data, generated_at=END.strftime("%Y-%m-%d 06:00:00 UTC"))
