"""Tests: passive volume, freshness, and schema monitors."""

from __future__ import annotations

import dataclasses
import datetime
import typing as t
from pathlib import Path

import pytest
from conftest import PLAN_EXECUTION_TIME, Probe

if t.TYPE_CHECKING:
    from sqlmesh.core.context import Context

    from sqlmesh_observability.collector import Collector

SCHEMA = "observability"
ONE_ROW = "SELECT CAST(@start_ds AS DATE) AS ds, 1 AS id"


@pytest.fixture
def collector() -> t.Iterator["Collector"]:
    from sqlmesh_observability import collector as collector_module

    collector_module._reset_for_tests()
    yield collector_module.get_collector()
    collector_module._reset_for_tests()


def _install_on_load(probe: Probe, **kwargs: t.Any) -> None:
    from sqlmesh_observability import install

    probe.hook = lambda config: install(config, "local", **kwargs)


def _model(
    project: Path,
    select: str,
    audits: str = "not_null(columns := (id))",
    name: str = "obs.events",
    filename: str = "events.sql",
    kind: str = "INCREMENTAL_BY_TIME_RANGE (time_column ds, batch_size 1)",
) -> None:
    audits_line = f",\n  audits ({audits})" if audits else ""
    (project / "models" / filename).write_text(
        f"""
MODEL (
  name {name},
  kind {kind},
  cron '@daily'{audits_line}
);

{select}
"""
    )


def _plan(context: "Context", environment: str = "prod", **kwargs: t.Any) -> t.Any:
    kwargs.setdefault("execution_time", PLAN_EXECUTION_TIME)
    return context.plan(environment, auto_apply=True, no_prompts=True, **kwargs)


def _rows(context: "Context", table: str, columns: str, where: str = "1=1") -> t.List[t.Tuple]:
    return context.engine_adapter.fetchall(
        f"SELECT {columns} FROM {SCHEMA}.{table} WHERE {where} ORDER BY ALL"
    )


def _table_exists(context: "Context", table: str) -> bool:
    from sqlglot import exp

    return context.engine_adapter.table_exists(exp.to_table(f"{SCHEMA}.{table}"))


def _day(value: t.Any) -> str:
    return str(value)[:10]


# --- 1.2 Collector copies plain metadata ----------------------------------------------------------


def _is_plain(value: t.Any) -> bool:
    if isinstance(value, (str, int, float, bool, type(None), datetime.datetime)):
        return True
    if isinstance(value, tuple):
        return all(_is_plain(item) for item in value)
    return False


def test_collector_copies_plain_model_and_schema_metadata(
    project: Path, probe: Probe, collector: "Collector"
) -> None:
    from sqlmesh.core.context import Context

    _install_on_load(probe)
    context = Context(paths=str(project))
    _plan(context)
    (project / "models" / "events.sql").write_text(
        (project / "models" / "events.sql").read_text().replace("1 AS id", "1 AS id, 2 AS extra")
    )
    context.load()
    _plan(context, forward_only=True)
    context.close()

    first, second = collector.finished
    run = first.models[0]
    assert run.kind == "INCREMENTAL_BY_TIME_RANGE"
    assert run.columns == (("ds", "DATE"), ("id", "INT"))
    assert run.time_column == "ds"

    (created,) = first.schemas
    assert created.action == "create"
    assert created.columns == (("ds", "DATE"), ("id", "INT"))
    migrated = [s for s in second.schemas if s.action == "schema_migration"][-1]
    assert migrated.version == created.version
    assert created.snapshot_id in migrated.previous_snapshot_ids
    assert [c for c, _ in migrated.columns] == ["ds", "id", "extra"]

    for invocation in (first, second):
        for record in [*invocation.models, *invocation.schemas]:
            for field in dataclasses.fields(record):
                value = getattr(record, field.name)
                assert _is_plain(value), (type(record).__name__, field.name, type(value))


# --- 2.1 Warehouse metrics and schema rows --------------------------------------------------------


@pytest.mark.usefixtures("collector")
def test_default_install_writes_no_passive_rows(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context

    _install_on_load(probe)
    context = Context(paths=str(project))
    _plan(context)
    for table in ("metrics", "schema_columns", "schema_changes"):
        assert not _table_exists(context, table) or not _rows(context, table, "*")
    context.close()


@pytest.mark.usefixtures("collector")
def test_metrics_latest_wins_on_restatement(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context

    _install_on_load(probe, monitors={"volume": True})
    _model(project, "SELECT CAST(@start_ds AS DATE) AS ds, id FROM raw.src")
    context = Context(paths=str(project))
    context.engine_adapter.execute("CREATE SCHEMA IF NOT EXISTS raw")
    context.engine_adapter.execute("CREATE TABLE raw.src AS SELECT range AS id FROM range(3)")
    context.load()
    _plan(context)

    rows = _rows(context, "metrics", "interval_start, metric_value, column_name")
    assert [(_day(r[0]), r[1], r[2]) for r in rows] == [
        ("2024-01-01", 3.0, ""),
        ("2024-01-02", 3.0, ""),
    ]

    for size in (5, 7):
        context.engine_adapter.execute("DELETE FROM raw.src")
        context.engine_adapter.execute(f"INSERT INTO raw.src SELECT range FROM range({size})")
        _plan(context, restate_models=["obs.events"], start="2024-01-01", end="2024-01-01")

    rows = _rows(context, "metrics", "interval_start, metric_value", "metric_name = 'row_count'")
    assert [(_day(r[0]), r[1]) for r in rows] == [("2024-01-01", 7.0), ("2024-01-02", 3.0)]
    context.close()


@pytest.mark.usefixtures("collector")
def test_failed_interval_skips_volume(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context
    from sqlmesh.utils.errors import PlanError

    _install_on_load(probe, monitors={"volume": True})
    _model(project, "SELECT CAST(@start_ds AS DATE) AS ds, CAST(NULL AS INT) AS id FROM range(5)")
    context = Context(paths=str(project))
    with pytest.raises(PlanError):
        _plan(context)
    assert _rows(context, "audit_results", "status, blocking") and all(
        status == "fail" for status, _ in _rows(context, "audit_results", "status, blocking")
    )
    assert not _table_exists(context, "metrics") or not _rows(context, "metrics", "*")
    context.close()


@pytest.mark.usefixtures("collector")
def test_freshness_recorded_when_column_exists_and_skipped_when_absent(
    project: Path, probe: Probe
) -> None:
    from sqlmesh.core.context import Context

    _install_on_load(probe, monitors={"freshness": {"column": "updated_at"}})
    _model(
        project,
        "SELECT CAST(@start_ds AS DATE) AS ds, 1 AS id, CAST(@start_ds AS TIMESTAMP) AS updated_at",
    )
    _model(project, ONE_ROW, name="obs.plain", filename="plain.sql")
    context = Context(paths=str(project))
    _plan(context)

    rows = _rows(context, "metrics", "model_name, metric_name, metric_value, error_message")
    assert {r[0].split(".")[-1].strip('"') for r in rows} == {"events"}
    assert {r[1] for r in rows} == {"freshness_lag_seconds"}
    # Plan execution time 2024-01-03 00:00; MAX(updated_at) is 2024-01-02 00:00.
    assert {r[2] for r in rows} == {86400.0}
    assert all(r[3] is None for r in rows)
    context.close()


@pytest.mark.usefixtures("collector")
def test_freshness_per_model_column_override(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context

    _install_on_load(
        probe,
        monitors={"freshness": {"column": "updated_at", "columns": {"obs.plain": "loaded_at"}}},
    )
    _model(
        project,
        "SELECT CAST(@start_ds AS DATE) AS ds, 1 AS id, CAST(@start_ds AS TIMESTAMP) AS loaded_at",
        name="obs.plain",
        filename="plain.sql",
    )
    context = Context(paths=str(project))
    _plan(context)
    rows = _rows(context, "metrics", "model_name, metric_name", "metric_value IS NOT NULL")
    assert {r[0].split(".")[-1].strip('"') for r in rows} == {"plain"}
    context.close()


@pytest.mark.usefixtures("collector")
def test_view_models_get_no_row_count(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context

    _install_on_load(probe, monitors={"volume": True})
    _model(project, "SELECT DATE '2024-01-01' AS ds, 1 AS id", kind="VIEW", audits="")
    context = Context(paths=str(project))
    _plan(context)
    assert not _table_exists(context, "metrics") or not _rows(context, "metrics", "*")
    context.close()


@pytest.mark.usefixtures("collector")
def test_schema_diff_on_new_version(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context

    _install_on_load(probe, monitors={"schema": True})
    _model(
        project,
        "SELECT CAST(@start_ds AS DATE) AS ds, CAST(1 AS INT) AS id, 'x' AS old_col",
    )
    context = Context(paths=str(project))
    _plan(context)
    assert len(_rows(context, "schema_columns", "column_name, data_type, ordinal")) == 3
    assert not _rows(context, "schema_changes", "*")

    _model(
        project,
        "SELECT CAST(@start_ds AS DATE) AS ds, CAST(1 AS BIGINT) AS id, 'y' AS new_col",
    )
    context.load()
    _plan(context)
    changes = _rows(context, "schema_changes", "column_name, change_type, old_type, new_type")
    assert changes == [
        ("id", "retyped", "INT", "BIGINT"),
        ("new_col", "added", None, "TEXT"),
        ("old_col", "removed", "TEXT", None),
    ]
    (versions,) = {
        (r[0], r[1]) for r in _rows(context, "schema_changes", "from_version, to_version, 1")
    }
    assert versions[0] != versions[1]

    # Replaying the same plan writes nothing new (idempotent per snapshot).
    _plan(context)
    assert len(_rows(context, "schema_changes", "*")) == 3
    context.close()


@pytest.mark.usefixtures("collector")
def test_forward_only_schema_change_recorded(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context

    _install_on_load(probe, monitors={"schema": True})
    context = Context(paths=str(project))
    _plan(context)
    (project / "models" / "events.sql").write_text(
        (project / "models" / "events.sql").read_text().replace("1 AS id", "1 AS id, 2 AS extra")
    )
    context.load()
    _plan(context, forward_only=True)
    rows = _rows(
        context,
        "schema_changes",
        "column_name, change_type, from_version, to_version, from_snapshot_id, to_snapshot_id",
    )
    assert [(r[0], r[1]) for r in rows] == [("extra", "added")]
    (_, _, from_version, to_version, from_id, to_id) = rows[0]
    assert from_version == to_version
    assert from_id != to_id
    context.close()


@pytest.mark.usefixtures("collector")
def test_base_rows_are_committed_before_metric_queries(
    project: Path, probe: Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlglot import exp
    from sqlmesh.core.context import Context

    from sqlmesh_observability import monitors

    seen: t.List[int] = []

    def exploding(invocation: t.Any, adapter: t.Any, log_adapter: t.Any, *args: t.Any) -> t.Any:
        table = exp.to_table(f"{SCHEMA}.invocations")
        query = (
            exp.select("COUNT(*)")
            .from_(table)
            .where(exp.column("invocation_id").eq(exp.Literal.string(invocation.invocation_id)))
        )
        seen.append(log_adapter.fetchone(query)[0])
        raise RuntimeError("metric query blew up")

    monkeypatch.setattr(monitors, "collect_metrics", exploding)
    _install_on_load(probe, monitors={"volume": True})
    context = Context(paths=str(project))
    _plan(context)  # still succeeds
    assert seen == [1]
    assert len(_rows(context, "audit_results", "*")) == 2
    context.close()


# --- 3.1 Read-time series, scoring, and report ----------------------------------------------------


def _days(values: t.Sequence[t.Optional[float]]) -> t.List[t.Tuple[datetime.datetime, t.Any]]:
    start = datetime.datetime(2024, 1, 1)
    return [(start + datetime.timedelta(days=i), v) for i, v in enumerate(values)]


def test_read_time_anomaly_uses_training_window() -> None:
    from sqlmesh_observability.monitors import ScoreSettings, score_series

    values = [1000.0] * 10 + [10.0, 11.0, 10.0, 12.0, 10.0, 11.0, 10.0, 12.0, 10.0, 11.0] * 2
    values = values[:29] + [11.0]
    scored = score_series(_days(values), ScoreSettings())
    assert scored[-1].anomaly is False

    spiked = values[:29] + [500.0]
    assert score_series(_days(spiked), ScoreSettings())[-1].anomaly is True


def test_historical_points_scored_against_own_past() -> None:
    from sqlmesh_observability.monitors import ScoreSettings, score_series

    values = [10.0, 11.0, 10.0, 12.0, 10.0, 11.0, 10.0, 12.0, 10.0, 500.0] + [11.0] * 20
    scored = score_series(_days(values), ScoreSettings())
    assert scored[9].anomaly is True
    assert scored[9].training_points == 9
    assert all(point.anomaly is False for point in scored[:7])  # too little history


def test_small_changes_ignored_with_percent_threshold() -> None:
    from sqlmesh_observability.monitors import ScoreSettings, score_series

    values = [1000.0] * 14 + [1001.0]
    assert score_series(_days(values), ScoreSettings())[-1].anomaly is True
    tolerant = ScoreSettings(spike_failure_percent_threshold=10)
    assert score_series(_days(values), tolerant)[-1].anomaly is False
    # The spike threshold does not apply to drops.
    dropped = [1000.0] * 14 + [999.0]
    assert score_series(_days(dropped), tolerant)[-1].anomaly is True


def test_normalize_monitor_settings() -> None:
    from sqlmesh_observability.monitors import normalize_monitors

    assert normalize_monitors(None).enabled is False
    settings = normalize_monitors(
        {
            "volume": {"threshold": 2, "spike_failure_percent_threshold": 20},
            "freshness": {"column": "updated_at", "columns": {"a.b": "ts"}},
            "schema": True,
        }
    )
    assert settings.volume is not None and settings.volume.threshold == 2
    assert settings.volume.spike_failure_percent_threshold == 20
    assert settings.freshness is not None and settings.freshness.column_for('"db"."a"."b"') == "ts"
    assert settings.freshness.column_for('"db"."a"."c"') == "updated_at"
    assert settings.schema is True


@pytest.mark.usefixtures("collector")
def test_promoted_version_metrics_in_prod_series(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context

    from sqlmesh_observability.reader import load_report_data

    _install_on_load(probe, monitors={"volume": True})
    context = Context(paths=str(project))
    _plan(context)
    _model(project, "SELECT CAST(@start_ds AS DATE) AS ds, id FROM range(4) AS r(id)")
    context.load()
    _plan(context, "dev", start="2024-01-01", end="2024-01-02")
    _plan(context)  # virtual update
    context.close()

    data = load_report_data(project)
    (model,) = data.models
    (series,) = [s for s in model.metrics if s.metric_name == "row_count"]
    assert [(p.interval_start[:10], p.value) for p in series.points] == [
        ("2024-01-01", 4.0),
        ("2024-01-02", 4.0),
    ]


@pytest.mark.usefixtures("collector")
def test_report_renders_series_anomaly_and_schema_change(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context

    from sqlmesh_observability.reader import load_report_data
    from sqlmesh_observability.report import render_report

    _install_on_load(probe, monitors={"volume": True, "schema": True})
    _model(project, "SELECT CAST(@start_ds AS DATE) AS ds, id FROM raw.src")
    context = Context(paths=str(project))
    context.engine_adapter.execute("CREATE SCHEMA IF NOT EXISTS raw")
    context.engine_adapter.execute("CREATE TABLE raw.src AS SELECT range AS id FROM range(10)")
    context.load()
    _plan(context, execution_time="2024-01-09")  # intervals 2024-01-01 .. 2024-01-08
    context.engine_adapter.execute("INSERT INTO raw.src SELECT range FROM range(10, 500)")
    context.run(execution_time="2024-01-10")  # adds 2024-01-09

    _model(project, "SELECT CAST(@start_ds AS DATE) AS ds, id, 1 AS added_col FROM raw.src")
    context.load()
    _plan(context, forward_only=True, execution_time="2024-01-10")
    context.close()

    data = load_report_data(project)
    (model,) = data.models
    (series,) = [s for s in model.metrics if s.metric_name == "row_count"]
    assert series.points[-1].value == 500.0 and series.points[-1].anomaly is True
    assert [a.model_name for a in data.anomalies] == [model.model_name]
    assert [(c.column_name, c.change_type) for c in model.schema_changes] == [
        ("added_col", "added")
    ]

    html = render_report(data)
    assert "500" in html
    assert 'id="overview-anomalies"' in html
    assert "added_col" in html


@pytest.mark.usefixtures("collector")
def test_upgrade_from_schema_version_1(project: Path, probe: Probe) -> None:
    """A version-1 log (no metric tables) is upgraded in place on the next write."""
    from sqlmesh.core.context import Context

    from sqlmesh_observability.writer import SCHEMA_VERSION

    _install_on_load(probe, monitors={"volume": True})
    context = Context(paths=str(project))
    context.engine_adapter.execute(f"CREATE SCHEMA {SCHEMA}")
    context.engine_adapter.execute(f"CREATE TABLE {SCHEMA}.schema_version (version INT)")
    context.engine_adapter.execute(f"INSERT INTO {SCHEMA}.schema_version VALUES (1)")
    _plan(context)
    assert _rows(context, "schema_version", "version") == [(SCHEMA_VERSION,)]
    assert len(_rows(context, "metrics", "*")) == 2
    context.close()
