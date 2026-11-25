"""End to end: one prod-shaped history, checked on every surface.

DuckDB only, no slow or docker marker.

History:
1. Prod plan (days 1-14). ``obs.summary``'s ``accepted_values`` fails, non-blocking.
2. Prod run adds day 15 with 50x the rows: ``volume_anomaly`` fails and ``row_count`` is anomalous.
   A scheduled ``monitor`` pages both owners.
3. A dev plan of ``obs.daily`` v2 fails its blocking ``not_null``. v2 is never promoted.
4. A dev change to ``obs.summary`` is built in dev, then promoted to prod by a virtual update.

Then the warehouse rows, the report, the app, and ``monitor`` must agree. The dev ``not_null``
failure is nowhere, and a second ``monitor`` run sends nothing.
"""

from __future__ import annotations

import typing as t
from pathlib import Path

import pytest
from conftest import Probe
from test_alerts import SENT, _target, clear_sent  # noqa: F401  (clear_sent is a fixture)
from test_report import _install_on_load, _plan, collector  # noqa: F401

SCHEMA = "observability"

DAILY_V1 = """
MODEL (
  name obs.daily,
  owner 'data-eng',
  kind INCREMENTAL_BY_TIME_RANGE (time_column ds, batch_size 1),
  cron '@daily',
  audits (
    not_null(columns := (id)),
    always_skipped,
    volume_anomaly(time_column := ds, source := obs.daily)
  )
);

SELECT ds, __ID__ AS id, status FROM raw.events WHERE ds BETWEEN @start_ds AND @end_ds
"""

SUMMARY = """
MODEL (
  name obs.summary,
  owner 'analytics',
  kind FULL,
  cron '@daily',
  audits (accepted_values_non_blocking(column := status, is_in := ('ok', 'pending')))
);

SELECT status, COUNT(*) AS n__EXTRA__ FROM obs.daily GROUP BY status
"""


def _write(project: Path, daily_id: str = "id", summary_extra: str = "") -> None:
    (project / "models" / "daily.sql").write_text(DAILY_V1.replace("__ID__", daily_id))
    (project / "models" / "summary.sql").write_text(SUMMARY.replace("__EXTRA__", summary_extra))


def _add_days(context: t.Any, first: int, last: int, rows: int) -> None:
    for day in range(first, last + 1):
        context.engine_adapter.execute(
            f"INSERT INTO raw.events SELECT DATE '2024-01-{day:02d}', i, "
            "CASE WHEN i = 0 THEN 'bad' ELSE 'ok' END FROM range("
            f"{rows}) t(i)"
        )


def _monitor(project: Path) -> int:
    from sqlmesh_observability import cli

    return cli.main(["monitor", "--path", str(project)])


def _sent_by_owner() -> t.Dict[t.Optional[str], str]:
    return {kwargs["owner"]: msg for _, msg, kwargs in SENT}


@pytest.mark.usefixtures("collector")
def test_e2e_prod_report_and_alert(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context
    from sqlmesh.utils.errors import PlanError
    from starlette.testclient import TestClient

    from sqlmesh_observability.app import create_app
    from sqlmesh_observability.audits import copy_audits
    from sqlmesh_observability.reader import load_report_data
    from sqlmesh_observability.report import render_report

    _install_on_load(probe, monitors={"volume": True}, sample_rows=2, alert_target=_target())
    (project / "models" / "events.sql").unlink()
    copy_audits(project)
    (project / "audits" / "always_skipped.sql").write_text(
        "AUDIT (name always_skipped, skip true);\nSELECT * FROM @this_model WHERE FALSE"
    )
    _write(project)
    context = Context(paths=str(project))
    context.engine_adapter.execute("CREATE SCHEMA IF NOT EXISTS raw")
    context.engine_adapter.execute("CREATE TABLE raw.events (ds DATE, id INT, status TEXT)")
    _add_days(context, 1, 14, rows=10)

    # 1. Prod plan: days 1-14. accepted_values fails, non-blocking, so the plan promotes.
    context.load()
    _plan(context, execution_time="2024-01-15")
    # 2. Prod run: day 15 spikes.
    _add_days(context, 15, 15, rows=500)
    context.run(execution_time="2024-01-16")
    assert SENT == [], "the alert target is never called by a plan or run"

    # The scheduled monitor pages both owners for the prod issues.
    assert _monitor(project) == 0
    first = _sent_by_owner()
    assert set(first) == {"analytics", "data-eng"}
    assert "accepted_values_non_blocking" in first["analytics"]
    assert "volume_anomaly" in first["data-eng"] and "2024-01-15" in first["data-eng"]
    assert "row_count anomaly" in first["data-eng"] and "500" in first["data-eng"]
    assert all("not_null" not in msg for msg in first.values())
    SENT.clear()

    # 3. A dev-only change to obs.daily fails its blocking not_null in dev.
    _write(project, daily_id="CAST(NULL AS INT)")
    context.load()
    with pytest.raises(PlanError):
        _plan(context, "dev", start="2024-01-01", end="2024-01-02", execution_time="2024-01-16")
    # 4. Revert it; change obs.summary in dev, then promote it to prod by a virtual update.
    _write(project, summary_extra=", 1 AS v")
    context.load()
    _plan(context, "dev", execution_time="2024-01-16")
    _plan(context, execution_time="2024-01-16")
    context.close()

    # Warehouse rows.
    import duckdb

    db = duckdb.connect(str(project.parent / "warehouse.duckdb"), read_only=True)
    try:
        rows = db.execute(
            "SELECT model_name, audit_name, environment, status, blocking, "
            f"CAST(interval_start AS DATE) FROM {SCHEMA}.audit_results"
        ).fetchall()
        spike = db.execute(
            f"SELECT metric_value FROM {SCHEMA}.metrics "
            "WHERE model_name LIKE '%daily%' AND metric_name = 'row_count' "
            "AND CAST(interval_start AS DATE) = DATE '2024-01-15'"
        ).fetchall()
    finally:
        db.close()

    def statuses(audit: str, environment: str) -> t.Set[str]:
        return {r[3] for r in rows if r[1] == audit and r[2] == environment}

    assert statuses("not_null", "prod") == {"pass"}
    assert statuses("always_skipped", "prod") == {"skip"}
    assert statuses("accepted_values_non_blocking", "prod") == {"fail"}
    assert "fail" in statuses("not_null", "dev")
    ((volume_status, volume_blocking),) = {
        (r[3], r[4])
        for r in rows
        if r[1] == "volume_anomaly" and str(r[5]) == "2024-01-15" and r[2] == "prod"
    }
    assert (volume_status, volume_blocking) == ("fail", False)
    assert [value for (value,) in spike] == [500.0]

    # Report.
    data = load_report_data(project)
    incidents = {(i.model_name.split(".")[-1].strip('"'), i.audit_name) for i in data.incidents}
    assert ("summary", "accepted_values_non_blocking") in incidents
    assert ("daily", "volume_anomaly") in incidents
    assert not any(audit == "not_null" for _, audit in incidents)
    assert [(a.metric_name, a.point.value) for a in data.anomalies] == [("row_count", 500.0)]
    summary = next(m for m in data.models if m.model_name.endswith('"summary"'))
    assert {a.environment for a in summary.audits} == {"dev"}, "v2 was built in dev, then promoted"
    html = render_report(data)
    assert "accepted_values_non_blocking" in html and "volume_anomaly" in html

    # App.
    client = TestClient(create_app(project))
    overview = client.get("/")
    assert overview.status_code == 200
    assert "accepted_values_non_blocking" in overview.text and "volume_anomaly" in overview.text
    assert client.get("/models/obs.daily").status_code == 200
    assert client.post("/").status_code == 405

    # Monitor: the dev failure and the dev-built summary result are not paged, and nothing else
    # is new. A second run sends nothing either.
    assert _monitor(project) == 0
    assert SENT == []
    assert _monitor(project) == 0
    assert SENT == []
