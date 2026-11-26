"""Tests: column monitors."""

from __future__ import annotations

import typing as t
from pathlib import Path

import pytest

if t.TYPE_CHECKING:
    from sqlglot import exp
    from sqlmesh.core.context import Context

    from sqlmesh_observability.collector import Collector

SCHEMA = "observability"

MODEL_SQL = """
MODEL (
  name obs.orders,
  kind INCREMENTAL_BY_TIME_RANGE (time_column ds, batch_size 1, batch_concurrency 1),
  cron '@daily',
  start '2024-01-01'__AUDITS__
);

SELECT CAST(ds AS DATE) AS ds, CAST(id AS INT) AS id, CAST(status AS TEXT) AS status,
  CAST(amount AS DOUBLE) AS amount
FROM raw.orders
WHERE ds BETWEEN @start_ds AND @end_ds
"""

FAILURES: t.List[t.Tuple[str, "exp.Query"]] = []


def _recording_target() -> t.Any:
    from sqlmesh.core.notification_target import GenericNotificationTarget

    class AuditFailures(GenericNotificationTarget):
        def notify_audit_failure(self, audit_error: t.Any, *args: t.Any, **kwargs: t.Any) -> None:
            FAILURES.append((audit_error.audit_name, audit_error.query))

    return AuditFailures(notify_on=["audit_failure"])


@pytest.fixture(autouse=True)
def clear_failures() -> t.Iterator[None]:
    FAILURES.clear()
    yield
    FAILURES.clear()


class OrdersProject:
    """A DuckDB project whose ``obs.orders`` model reads ``raw.orders``, filled day by day."""

    def __init__(self, root: Path, audits: str = "", monitors: t.Any = None) -> None:
        from sqlmesh.core.config import (
            Config,
            DuckDBConnectionConfig,
            GatewayConfig,
            ModelDefaultsConfig,
        )
        from sqlmesh.core.context import Context

        from sqlmesh_observability import install
        from sqlmesh_observability.audits import copy_audits

        self.root = root
        (root / "models").mkdir(parents=True)
        audits_line = f",\n  audits ({audits})" if audits else ""
        (root / "models" / "orders.sql").write_text(MODEL_SQL.replace("__AUDITS__", audits_line))
        copy_audits(root)
        config = Config(
            gateways={
                "local": GatewayConfig(
                    connection=DuckDBConnectionConfig(database=str(root / "db.duckdb"))
                )
            },
            default_gateway="local",
            model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        )
        config.notification_targets.append(_recording_target())
        if monitors is not None:
            install(config, "local", monitors=monitors)
        self.context: "Context" = Context(paths=str(root), config=config)
        self.context.engine_adapter.execute("CREATE SCHEMA IF NOT EXISTS raw")
        self.context.engine_adapter.execute(
            "CREATE TABLE raw.orders (ds DATE, id INT, status TEXT, amount DOUBLE)"
        )

    def add_days(
        self,
        first: int,
        last: int,
        rows: int = 10,
        null_status: int = 0,
        amount: t.Optional[float] = 10.0,
    ) -> None:
        """``rows`` rows a day for 2024-01-<first>..<last>; ``null_status`` of them lack status."""
        value = "NULL" if amount is None else str(amount)
        for day in range(first, last + 1):
            self.context.engine_adapter.execute(
                f"INSERT INTO raw.orders SELECT DATE '2024-01-{day:02d}', i, "
                f"CASE WHEN i < {null_status} THEN NULL ELSE 'ok' END, {value} "
                f"FROM range({rows}) t(i)"
            )

    def plan(self, execution_time: str) -> None:
        self.context.plan("prod", auto_apply=True, no_prompts=True, execution_time=execution_time)

    def run(self, execution_time: str) -> None:
        self.context.run(execution_time=execution_time)

    def rows(self, query: t.Any) -> t.List[t.Tuple[t.Any, ...]]:
        return self.context.engine_adapter.fetchall(query)


def _flagged_days(project: OrdersProject, name: str = "column_anomaly") -> t.List[str]:
    return [
        str(row[0])[:10]
        for audit_name, query in FAILURES
        if audit_name == name
        for row in project.rows(query)
    ]


def _stable_then_day16(tmp_path: Path, audits: str, **day16: t.Any) -> OrdersProject:
    project = OrdersProject(tmp_path / "project", audits)
    project.add_days(1, 15)
    project.plan(execution_time="2024-01-16")
    assert FAILURES == [], "stable history must not flag"
    project.add_days(16, 16, **day16)
    project.run(execution_time="2024-01-17")
    return project


NULL_RATE = (
    "column_anomaly(column := status, metric := null_rate, time_column := ds, source := obs.orders)"
)
MEAN = "column_anomaly(column := amount, metric := mean, time_column := ds, source := obs.orders)"


# --- 1.1 column_anomaly audit -----------------------------------------------------------------


def test_column_anomaly_flags_null_rate_jump(tmp_path: Path) -> None:
    project = _stable_then_day16(tmp_path, NULL_RATE, null_status=9)
    assert _flagged_days(project) == ["2024-01-16"]


def test_column_anomaly_flags_mean_drift(tmp_path: Path) -> None:
    project = _stable_then_day16(tmp_path, MEAN, amount=100.0)
    assert _flagged_days(project) == ["2024-01-16"]


def test_column_anomaly_ignores_stable_and_empty_days(tmp_path: Path) -> None:
    """An empty day has no value: it is not flagged and does not count as training."""
    _stable_then_day16(tmp_path, f"{NULL_RATE}, {MEAN}", rows=0)
    assert FAILURES == []


def test_column_anomaly_needs_history(tmp_path: Path) -> None:
    project = OrdersProject(tmp_path / "project", NULL_RATE)
    project.add_days(1, 5)
    project.plan(execution_time="2024-01-06")
    project.add_days(6, 6, null_status=9)
    project.run(execution_time="2024-01-07")
    assert FAILURES == []


def test_column_anomaly_rejects_unknown_metric(tmp_path: Path) -> None:
    """SQLMesh fails a plan on any audit error, blocking or not. It only logs the cause, so read
    the message from the package's own audit record."""
    from sqlmesh_observability import collector as collector_module

    collector_module._reset_for_tests()
    try:
        bad = NULL_RATE.replace("null_rate", "median")
        project = OrdersProject(tmp_path / "project", bad, monitors={})
        project.add_days(1, 2)
        with pytest.raises(Exception):
            project.plan(execution_time="2024-01-03")
        audits = [a for inv in collector_module.get_collector().finished for a in inv.audits]
        errors = [a for a in audits if a.audit_name == "column_anomaly"]
        assert errors and all(a.status == "error" for a in errors)
        assert "unknown metric 'median'" in (errors[0].error_message or "")
    finally:
        collector_module._reset_for_tests()


def test_column_anomaly_transpiles(tmp_path: Path) -> None:
    import sqlglot

    project = _stable_then_day16(tmp_path, f"{NULL_RATE}, {MEAN}", null_status=9, amount=100.0)
    assert len(FAILURES) == 2
    for _, query in FAILURES:
        expected = project.rows(query)
        assert expected
        for dialect in ("snowflake", "bigquery"):
            sql = query.sql(dialect=dialect, unsupported_level=sqlglot.ErrorLevel.RAISE)
            back = sqlglot.parse_one(sql, read=dialect).sql(
                dialect="duckdb", unsupported_level=sqlglot.ErrorLevel.RAISE
            )
            assert project.rows(back) == expected, (dialect, sql)


def test_init_audits_ships_column_anomaly() -> None:
    from sqlmesh_observability.audits import shipped_audits

    assert "column_anomaly.sql" in {str(getattr(p, "name", p)) for p in shipped_audits()}


# --- 2.1 Passive column metrics ---------------------------------------------------------------


@pytest.fixture
def collector() -> t.Iterator["Collector"]:
    from sqlmesh_observability import collector as collector_module

    collector_module._reset_for_tests()
    yield collector_module.get_collector()
    collector_module._reset_for_tests()


def _metrics(project: OrdersProject) -> t.List[t.Tuple[t.Any, ...]]:
    return project.rows(
        f"SELECT CAST(interval_start AS DATE), metric_name, column_name, metric_value, "
        f"error_message FROM {SCHEMA}.metrics ORDER BY ALL"
    )


def test_normalize_column_monitor_settings() -> None:
    from sqlmesh_observability.monitors import normalize_monitors

    settings = normalize_monitors(
        {"columns": {"orders": {"null_rate": ["status"], "mean": ["amount"], "median": ["x"]}}}
    )
    assert settings.enabled
    assert settings.columns_for('"db"."obs"."orders"') == (
        ("null_rate", "status"),
        ("mean", "amount"),
    )
    assert settings.columns_for('"db"."obs"."other"') == ()


@pytest.mark.usefixtures("collector")
def test_unconfigured_column_is_absent(tmp_path: Path) -> None:
    project = OrdersProject(
        tmp_path / "project", monitors={"columns": {"orders": {"null_rate": ["status"]}}}
    )
    project.add_days(1, 2, null_status=4)
    project.plan(execution_time="2024-01-03")
    assert {(r[1], r[2]) for r in _metrics(project)} == {("null_rate", "status")}
    assert [r[3] for r in _metrics(project)] == [0.4, 0.4]
    project.context.close()


@pytest.mark.usefixtures("collector")
def test_missing_column_records_metric_error(tmp_path: Path) -> None:
    project = OrdersProject(
        tmp_path / "project",
        monitors={"columns": {"orders": {"null_rate": ["nope", "status"]}}},
    )
    project.add_days(1, 1)
    project.plan(execution_time="2024-01-02")  # succeeds
    rows = {r[2]: r for r in _metrics(project)}
    assert rows["nope"][3] is None and "nope" in rows["nope"][4]
    assert rows["status"][3] == 0.0 and rows["status"][4] is None
    project.context.close()


@pytest.mark.usefixtures("collector")
def test_mean_all_nulls(tmp_path: Path) -> None:
    project = OrdersProject(
        tmp_path / "project", monitors={"columns": {"orders": {"mean": ["amount"]}}}
    )
    project.add_days(1, 1, amount=None)
    project.add_days(2, 2, amount=None, rows=0)
    project.plan(execution_time="2024-01-03")
    rows = _metrics(project)
    assert [(str(r[0]), r[3], r[4]) for r in rows] == [
        ("2024-01-01", None, None),
        ("2024-01-02", None, None),
    ]
    project.context.close()


# --- 2.1 / 3.1 Read-time anomaly and report ------------------------------------------------------


@pytest.mark.usefixtures("collector")
def test_column_null_rate_anomaly(tmp_path: Path) -> None:
    from sqlmesh_observability.reader import load_report_data
    from sqlmesh_observability.report import render_report

    project = OrdersProject(
        tmp_path / "project", monitors={"columns": {"orders": {"null_rate": ["status"]}}}
    )
    project.add_days(1, 14)
    project.plan(execution_time="2024-01-15")
    project.add_days(15, 15, null_status=9)
    project.run(execution_time="2024-01-16")
    project.context.close()

    data = _report_data(project, load_report_data)
    (anomaly,) = data.anomalies
    assert (anomaly.metric_name, anomaly.column_name) == ("null_rate", "status")
    assert anomaly.point.value == pytest.approx(0.9)

    html = render_report(data)
    assert "null_rate" in html
    assert "null_rate (status)" in html  # listed in the HTML fallback's anomalies


def _report_data(project: OrdersProject, load: t.Callable[..., t.Any]) -> t.Any:
    """Load report data for a project whose config is built in Python, not from config.py."""
    (project.root / "config.py").write_text(
        "from sqlmesh.core.config import Config, DuckDBConnectionConfig, GatewayConfig, "
        "ModelDefaultsConfig\n"
        "config = Config(\n"
        f"    gateways={{'local': GatewayConfig(connection=DuckDBConnectionConfig("
        f"database={str(project.root / 'db.duckdb')!r}))}},\n"
        "    default_gateway='local',\n"
        "    model_defaults=ModelDefaultsConfig(dialect='duckdb'),\n"
        ")\n"
    )
    return load(project.root)
