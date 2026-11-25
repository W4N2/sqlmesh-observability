"""Tests: the warehouse audit log.

The project's ``config.py`` calls ``install(config, "local", ...)`` at load time, exactly as a real
project does, and the tests read the package's tables back from the same DuckDB file.
"""

from __future__ import annotations

import json
import typing as t
from pathlib import Path

import pytest
from conftest import PLAN_EXECUTION_TIME, Probe

if t.TYPE_CHECKING:
    from sqlmesh.core.context import Context

    from sqlmesh_observability.collector import Collector

SCHEMA = "observability"


@pytest.fixture
def collector() -> t.Iterator["Collector"]:
    from sqlmesh_observability import collector as collector_module

    collector_module._reset_for_tests()
    yield collector_module.get_collector()
    collector_module._reset_for_tests()


def _install_on_load(probe: Probe, **kwargs: t.Any) -> None:
    from sqlmesh_observability import install

    probe.hook = lambda config: install(config, "local", **kwargs)


def _model(project: Path, select: str, audits: str) -> None:
    (project / "models" / "events.sql").write_text(
        f"""
MODEL (
  name obs.events,
  kind INCREMENTAL_BY_TIME_RANGE (time_column ds, batch_size 1),
  cron '@daily',
  audits ({audits})
);

{select}
"""
    )


ONE_ROW = "SELECT CAST(@start_ds AS DATE) AS ds, 1 AS id"
FIVE_NULL_ROWS = "SELECT CAST(@start_ds AS DATE) AS ds, CAST(NULL AS INT) AS id FROM range(5)"


def _plan(context: "Context", environment: str = "prod", **kwargs: t.Any) -> t.Any:
    kwargs.setdefault("execution_time", PLAN_EXECUTION_TIME)
    return context.plan(environment, auto_apply=True, no_prompts=True, **kwargs)


def _rows(context: "Context", table: str, columns: str, order: str = "1") -> t.List[t.Tuple]:
    return context.engine_adapter.fetchall(
        f"SELECT {columns} FROM {SCHEMA}.{table} ORDER BY {order}"
    )


def _count(context: "Context", table: str) -> int:
    return context.engine_adapter.fetchone(f"SELECT COUNT(*) FROM {SCHEMA}.{table}")[0]


def _day(value: t.Any) -> str:
    return str(value)[:10]


# --- Rows and statuses ----------------------------------------------------------------------------


@pytest.mark.usefixtures("collector")
def test_plan_writes_pass(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context

    _install_on_load(probe)
    context = Context(paths=str(project))
    plan = _plan(context)
    physical = context.table_name("obs.events", prod=True)

    assert _rows(context, "invocations", "command, environment, plan_id, status") == [
        ("plan", "prod", plan.plan_id, "success")
    ]
    from sqlmesh_observability.writer import SCHEMA_VERSION

    assert _rows(context, "schema_version", "version") == [(SCHEMA_VERSION,)]
    runs = _rows(
        context,
        "model_runs",
        "interval_start, interval_end, status, environment, physical_table, audit_only",
    )
    assert [(_day(s), _day(e)) for s, e, *_ in runs] == [
        ("2024-01-01", "2024-01-02"),
        ("2024-01-02", "2024-01-03"),
    ]
    assert {tuple(r[2:]) for r in runs} == {("success", "prod", physical, False)}
    audits = _rows(
        context,
        "audit_results",
        "interval_start, audit_name, status, violation_count, blocking, physical_table, args_json",
    )
    assert [_day(r[0]) for r in audits] == ["2024-01-01", "2024-01-02"]
    assert {tuple(r[1:6]) for r in audits} == {("not_null", "pass", 0, True, physical)}
    assert json.loads(audits[0][6]) == {"columns": "(id)"}


@pytest.mark.usefixtures("collector")
def test_plan_writes_blocking_fail(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context
    from sqlmesh.utils.errors import PlanError

    _install_on_load(probe)
    _model(project, FIVE_NULL_ROWS, "not_null(columns := (id))")
    context = Context(paths=str(project))

    with pytest.raises(PlanError):
        _plan(context)

    assert _rows(context, "invocations", "status") == [("failure",)]
    (audit,) = _rows(context, "audit_results", "status, violation_count, blocking, query_sql")[:1]
    assert audit[:3] == ("fail", 5, True)
    assert "IS NULL" in audit[3].upper()
    assert context.state_sync.get_environment("prod") is None  # not promoted


@pytest.mark.usefixtures("collector")
def test_non_blocking_records_failure_and_continues(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context

    _install_on_load(probe)
    _model(project, FIVE_NULL_ROWS, "not_null_non_blocking(columns := (id))")
    context = Context(paths=str(project))
    _plan(context)

    assert _rows(context, "invocations", "status") == [("success",)]
    assert {r for r in _rows(context, "model_runs", "status")} == {("success",)}
    audits = _rows(context, "audit_results", "audit_name, status, violation_count, blocking")
    assert set(audits) == {("not_null_non_blocking", "fail", 5, False)}


@pytest.mark.usefixtures("collector")
def test_skip_and_query_error(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context

    _install_on_load(probe)
    (project / "audits").mkdir()
    (project / "audits" / "always_skipped.sql").write_text(
        "AUDIT (name always_skipped, skip true);\nSELECT * FROM @this_model WHERE FALSE"
    )
    (project / "audits" / "broken.sql").write_text(
        "AUDIT (name broken);\nSELECT missing_column FROM @this_model"
    )
    _model(project, ONE_ROW, "not_null(columns := (id)), always_skipped, broken")
    context = Context(paths=str(project))

    with pytest.raises(Exception):
        _plan(context)

    by_name = {
        name: (status, error)
        for name, status, error in _rows(
            context, "audit_results", "audit_name, status, error_message"
        )
    }
    assert by_name["not_null"] == ("pass", None)
    assert by_name["always_skipped"] == ("skip", None)
    assert by_name["broken"][0] == "error"
    assert "missing_column" in by_name["broken"][1]


# --- When rows are written -----------------------------------------------------------------------


@pytest.mark.usefixtures("collector")
def test_flush_per_invocation(project: Path, probe: Probe) -> None:
    """Two applies on one Context, as a long-lived `sqlmesh ui` process does. No flush, no exit."""
    from sqlmesh.core.context import Context

    _install_on_load(probe)
    context = Context(paths=str(project))
    _plan(context)
    assert _count(context, "invocations") == 1

    _model(project, "SELECT CAST(@start_ds AS DATE) AS ds, 2 AS id", "not_null(columns := (id))")
    context.load()
    _plan(context)

    assert _count(context, "invocations") == 2


def test_idempotent_flush(project: Path, probe: Probe, collector: "Collector") -> None:
    from sqlmesh.core.context import Context

    _install_on_load(probe)
    context = Context(paths=str(project))
    _plan(context)
    counts = {t: _count(context, t) for t in ("invocations", "model_runs", "audit_results")}

    assert collector.writer is not None
    collector.writer.write(collector.finished[-1])  # replay the same invocation
    collector.writer.write(collector.finished[-1])

    assert {t: _count(context, t) for t in counts} == counts


def test_writer_failure_does_not_fail_the_run(
    project: Path, probe: Probe, collector: "Collector", monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlmesh.core.context import Context
    from sqlmesh.utils.errors import PlanError

    from sqlmesh_observability.writer import Writer

    def boom(self: t.Any, invocation: t.Any) -> None:
        raise RuntimeError("warehouse down")

    monkeypatch.setattr(Writer, "write", boom)
    _install_on_load(probe)
    context = Context(paths=str(project))
    _plan(context)  # passing plan still succeeds

    _model(project, FIVE_NULL_ROWS, "not_null(columns := (id))")
    context.load()
    with pytest.raises(PlanError) as raised:
        _plan(context)
    assert type(raised.value) is PlanError


# --- Samples ------------------------------------------------------------------------------------


@pytest.mark.parametrize("sample_rows", [0, 2])
@pytest.mark.usefixtures("collector")
def test_samples_opt_in(project: Path, probe: Probe, sample_rows: int) -> None:
    from sqlmesh.core.context import Context

    _install_on_load(probe, sample_rows=sample_rows)
    # One failing (5 bad rows per interval) and one passing audit.
    _model(
        project, FIVE_NULL_ROWS, "not_null_non_blocking(columns := (id)), not_null(columns := (ds))"
    )
    context = Context(paths=str(project))
    _plan(context)

    samples = _rows(context, "audit_samples", "audit_name, interval_start, row_json", "1, 2")
    if sample_rows == 0:
        assert samples == []
        return
    assert {name for name, _, _ in samples} == {"not_null_non_blocking"}
    per_interval: t.Dict[str, int] = {}
    for _, start, row_json in samples:
        per_interval[_day(start)] = per_interval.get(_day(start), 0) + 1
        assert json.loads(row_json)["id"] is None
    assert per_interval == {"2024-01-01": 2, "2024-01-02": 2}


# --- Facts follow the data ----------------------------------------------------------------------


@pytest.mark.usefixtures("collector")
def test_promoted_dev_version_is_prod(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context

    _install_on_load(probe)
    context = Context(paths=str(project))
    _plan(context)

    _model(project, "SELECT CAST(@start_ds AS DATE) AS ds, 2 AS id", "not_null(columns := (id))")
    context.load()
    _plan(context, "dev", start="2024-01-01", end="2024-01-02")  # builds the new version in dev
    _plan(context)  # promotes it to prod with a virtual update: no recompute

    environment = context.state_reader.get_environment("prod")
    assert environment is not None
    prod_versions = {s.version for s in environment.snapshots if s.name.endswith('"events"')}
    dev_audits = context.engine_adapter.fetchall(
        f"SELECT version FROM {SCHEMA}.audit_results WHERE environment = 'dev'"
    )
    assert dev_audits and {v for (v,) in dev_audits} <= prod_versions
    last_invocation = _rows(context, "invocations", "invocation_id, environment", "started_at")[-1]
    assert last_invocation[1] == "prod"
    assert (
        context.engine_adapter.fetchone(
            f"SELECT COUNT(*) FROM {SCHEMA}.model_runs WHERE invocation_id = '{last_invocation[0]}'"
        )[0]
        == 0
    )


# --- Scheduled use: one OS process per command --------------------------------------------------


def test_separate_cli_processes_persist_before_exit(tmp_path: Path) -> None:
    """What a scheduler does: `sqlmesh plan` and `sqlmesh run` as separate processes.

    Rows must be in the warehouse when each process has exited, with no atexit reliance in the
    test: the next process opens the database fresh.
    """
    import subprocess
    import sys

    import duckdb
    from conftest import EVENTS_SQL

    root = tmp_path / "project"
    (root / "models").mkdir(parents=True)
    (root / "models" / "events.sql").write_text(EVENTS_SQL)
    database = tmp_path / "warehouse.duckdb"
    (root / "config.py").write_text(
        f"""
from sqlmesh.core.config import Config, DuckDBConnectionConfig, GatewayConfig, ModelDefaultsConfig
from sqlmesh_observability import install

config = install(
    Config(
        gateways={{
            "local": GatewayConfig(connection=DuckDBConnectionConfig(database={str(database)!r}))
        }},
        default_gateway="local",
        model_defaults=ModelDefaultsConfig(dialect="duckdb", start="2024-01-01"),
    ),
    "local",
)
"""
    )
    # The installed console script, as a scheduler runs it (sqlmesh.cli.main has no __main__).
    sqlmesh = [str(Path(sys.executable).parent / "sqlmesh"), "--paths", str(root)]
    for args in (
        ["plan", "--auto-apply", "--no-prompts", "--execution-time", PLAN_EXECUTION_TIME],
        ["run", "--end", "2024-01-04"],
    ):
        done = subprocess.run(
            [*sqlmesh, *args],
            capture_output=True,
            text=True,
            timeout=300,
            env={**__import__("os").environ, "SQLMESH__DISABLE_ANONYMIZED_ANALYTICS": "1"},
        )
        assert done.returncode == 0, done.stdout + done.stderr

    with duckdb.connect(str(database), read_only=True) as connection:
        invocations = connection.execute(
            f"SELECT command, environment, status FROM {SCHEMA}.invocations ORDER BY started_at"
        ).fetchall()
        model_runs = connection.execute(f"SELECT COUNT(*) FROM {SCHEMA}.model_runs").fetchone()
    assert invocations == [("plan", "prod", "success"), ("run", "prod", "success")]
    assert model_runs == (4,)
