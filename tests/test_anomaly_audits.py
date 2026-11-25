"""Tests: package-shipped ``volume_anomaly`` / ``freshness_anomaly`` audits.

Each test builds a DuckDB project whose model reads a raw table the test fills day by day, copies
the package audits into ``audits/`` (as ``sqlmesh-observe init-audits`` does), and reads audit
failures through SQLMesh's public ``audit_failure`` notification.
"""

from __future__ import annotations

import typing as t
from pathlib import Path

import pytest

if t.TYPE_CHECKING:
    from sqlglot import exp
    from sqlmesh.core.context import Context

MODEL_SQL = """
MODEL (
  name obs.daily,
  kind INCREMENTAL_BY_TIME_RANGE (time_column ds, batch_size 1, batch_concurrency 1),
  cron '@daily',
  start '2024-01-01',
  audits (__AUDITS__)
);

SELECT CAST(ds AS DATE) AS ds, CAST(id AS INT) AS id__EXTRA__
FROM raw.events
WHERE ds BETWEEN @start_ds AND @end_ds
"""

VOLUME = "volume_anomaly(time_column := ds, source := obs.daily)"
FRESHNESS = "freshness_anomaly(time_column := ds, source := obs.daily, warn_after_hours := 36)"

FAILURES: t.List[t.Tuple[str, int, "exp.Query"]] = []


def _recording_target() -> t.Any:
    from sqlmesh.core.notification_target import GenericNotificationTarget

    class AuditFailures(GenericNotificationTarget):
        def notify_audit_failure(self, audit_error: t.Any, *args: t.Any, **kwargs: t.Any) -> None:
            FAILURES.append((audit_error.audit_name, audit_error.count, audit_error.query))

    return AuditFailures(notify_on=["audit_failure"])


class AuditProject:
    def __init__(self, root: Path, audits: str) -> None:
        from sqlmesh.core.config import (
            Config,
            DuckDBConnectionConfig,
            GatewayConfig,
            ModelDefaultsConfig,
        )
        from sqlmesh.core.context import Context

        from sqlmesh_observability.audits import copy_audits

        self.root = root
        (root / "models").mkdir(parents=True)
        self.write_model(audits)
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
        self.context: "Context" = Context(paths=str(root), config=config)
        self.context.engine_adapter.execute("CREATE SCHEMA IF NOT EXISTS raw")
        self.context.engine_adapter.execute("CREATE TABLE raw.events (ds DATE, id INT)")

    def write_model(self, audits: str, extra: str = "") -> None:
        sql = MODEL_SQL.replace("__AUDITS__", audits).replace("__EXTRA__", extra)
        (self.root / "models" / "daily.sql").write_text(sql)

    def add_days(self, first: int, last: int, rows: int) -> None:
        """Insert ``rows`` rows for each day 2024-01-<first>..<last>."""
        for day in range(first, last + 1):
            if rows:
                self.context.engine_adapter.execute(
                    f"INSERT INTO raw.events SELECT DATE '2024-01-{day:02d}', i "
                    f"FROM range({rows}) t(i)"
                )

    def plan(self, execution_time: str) -> None:
        self.context.plan("prod", auto_apply=True, no_prompts=True, execution_time=execution_time)

    def run(self, execution_time: str) -> None:
        self.context.run(execution_time=execution_time)

    def rows(self, query: "exp.Query") -> t.List[t.Tuple[t.Any, ...]]:
        return self.context.engine_adapter.fetchall(query)


@pytest.fixture(autouse=True)
def clear_failures() -> t.Iterator[None]:
    FAILURES.clear()
    yield
    FAILURES.clear()


def _project(tmp_path: Path, audits: str) -> AuditProject:
    return AuditProject(tmp_path / "project", audits)


def _stable_then(tmp_path: Path, audits: str, day16_rows: int) -> AuditProject:
    """15 stable days through a plan, then day 16 with ``day16_rows`` through a run."""
    project = _project(tmp_path, audits)
    project.add_days(1, 15, rows=10)
    project.plan(execution_time="2024-01-16")
    assert FAILURES == [], "stable history must not flag"
    project.add_days(16, 16, rows=day16_rows)
    project.run(execution_time="2024-01-17")
    return project


def _flagged_days(project: AuditProject, name: str) -> t.List[str]:
    days = []
    for audit_name, _, query in FAILURES:
        if audit_name == name:
            days.extend(str(row[0])[:10] for row in project.rows(query))
    return days


# --- volume_anomaly -------------------------------------------------------------------------------


def test_volume_anomaly_audit_flags_spike(tmp_path: Path) -> None:
    project = _stable_then(tmp_path, VOLUME, day16_rows=100)

    assert [(name, count) for name, count, _ in FAILURES] == [("volume_anomaly", 1)]
    assert _flagged_days(project, "volume_anomaly") == ["2024-01-16"]
    # `source :=` resolved to the version's physical table, not the prod view.
    assert _source_tables(FAILURES[0][2]) == {project.context.table_name("obs.daily", prod=True)}


def _source_tables(query: "exp.Query") -> t.Set[str]:
    from sqlglot import exp

    ctes = {cte.alias for cte in query.find_all(exp.CTE)}
    return {
        ".".join(part.name for part in table.parts)
        for table in query.find_all(exp.Table)
        if table.name not in ctes and table.name != "days"
    }


def test_volume_anomaly_audit_flags_zero_rows(tmp_path: Path) -> None:
    project = _stable_then(tmp_path, VOLUME, day16_rows=0)

    assert [(name, count) for name, count, _ in FAILURES] == [("volume_anomaly", 1)]
    assert _flagged_days(project, "volume_anomaly") == ["2024-01-16"]


def test_volume_anomaly_audit_needs_history(tmp_path: Path) -> None:
    project = _project(tmp_path, VOLUME)
    project.add_days(1, 5, rows=10)  # 5 training days < min_buckets (7)
    project.plan(execution_time="2024-01-06")
    project.add_days(6, 6, rows=1000)
    project.run(execution_time="2024-01-07")

    assert FAILURES == []


def test_volume_anomaly_audit_is_non_blocking_by_default(tmp_path: Path) -> None:
    project = _stable_then(tmp_path, VOLUME, day16_rows=100)

    # The run completed: day 16 is in the prod table despite the failure.
    assert project.context.engine_adapter.fetchone("SELECT COUNT(*) FROM obs.daily")[0] == 250


def test_volume_anomaly_audit_blocks_when_blocking(tmp_path: Path) -> None:
    from sqlmesh.utils.errors import PlanError

    blocking = "volume_anomaly(time_column := ds, source := obs.daily, blocking := true)"
    project = _project(tmp_path, blocking)
    project.add_days(1, 15, rows=10)
    project.plan(execution_time="2024-01-16")

    project.add_days(16, 16, rows=100)
    project.write_model(blocking, extra=", 1 AS v")  # breaking change: rebuilds a new version
    project.context.load()
    prod_table = project.context.table_name("obs.daily", prod=True)
    with pytest.raises(PlanError):
        project.plan(execution_time="2024-01-17")

    # The failure came from the new version's table, flagging the spike (not a missing day).
    (name, count, query), *_ = FAILURES
    assert (name, count) == ("volume_anomaly", 1)
    (table,) = _source_tables(query)
    assert table != prod_table and table.startswith("db.sqlmesh__obs.obs__daily__")
    assert project.rows(query)[0][1] == 100

    # Not promoted: prod still serves the previous version (no column v, no day 16).
    columns = project.context.engine_adapter.columns("obs.daily")
    assert "v" not in columns
    assert project.context.engine_adapter.fetchone("SELECT COUNT(*) FROM obs.daily")[0] == 150


@pytest.mark.parametrize(
    ("settings", "day16_rows", "flagged"),
    [
        # Constant history: by default any change is flagged (same as Elementary with >= 10 days).
        ("", 11, True),
        # spike_failure_percent_threshold ignores spikes smaller than that percent of the mean...
        ("spike_failure_percent_threshold := 50", 11, False),
        ("spike_failure_percent_threshold := 50", 100, True),
        # ...but only in its own direction: drops are still judged by the z-score alone.
        ("spike_failure_percent_threshold := 50", 8, True),
        ("drop_failure_percent_threshold := 50", 8, False),
        ("drop_failure_percent_threshold := 50", 0, True),
        ("drop_failure_percent_threshold := 50", 11, True),
        (
            "spike_failure_percent_threshold := 50, drop_failure_percent_threshold := 50",
            11,
            False,
        ),
    ],
)
def test_volume_anomaly_ignore_small_changes(
    tmp_path: Path, settings: str, day16_rows: int, flagged: bool
) -> None:
    """Elementary's ignore_small_changes (spike/drop_failure_percent_threshold), off by default.

    Mirrors elementary-data/dbt-data-reliability macros/edr/tests/test_utils/get_anomaly_query.sql
    ``avg_percent_anomalous_condition`` for anomaly_direction 'both'.
    """
    args = f", {settings}" if settings else ""
    audit = f"volume_anomaly(time_column := ds, source := obs.daily{args})"
    project = _stable_then(tmp_path, audit, day16_rows=day16_rows)

    assert _flagged_days(project, "volume_anomaly") == (["2024-01-16"] if flagged else [])
    if flagged:
        _assert_round_trip(project, FAILURES[0][2])


# --- freshness_anomaly ----------------------------------------------------------------------------


def test_freshness_anomaly_audit(tmp_path: Path) -> None:
    project = _project(tmp_path, FRESHNESS)
    project.add_days(1, 5, rows=10)
    project.plan(execution_time="2024-01-06")
    assert FAILURES == [], "a backfill whose data is current for each interval is fresh"

    project.run(execution_time="2024-01-09")  # no new rows for 06..08

    assert FAILURES and {name for name, _, _ in FAILURES} == {"freshness_anomaly"}
    assert all(count == 1 for _, count, _ in FAILURES)
    (row,) = project.rows(FAILURES[0][2])
    assert str(row[0]).startswith("2024-01-05")  # the latest data it found


# --- Portability ----------------------------------------------------------------------------------


def _assert_round_trip(project: AuditProject, query: "exp.Query") -> None:
    import sqlglot

    expected = project.rows(query)
    assert expected, "round trip needs a query that returns rows"
    for dialect in ("snowflake", "bigquery"):
        sql = query.sql(dialect=dialect, unsupported_level=sqlglot.ErrorLevel.RAISE)
        parsed = sqlglot.parse_one(sql, read=dialect)
        back = parsed.sql(dialect="duckdb", unsupported_level=sqlglot.ErrorLevel.RAISE)
        assert project.context.engine_adapter.fetchall(back) == expected, (dialect, sql)


def test_volume_anomaly_audit_transpiles(tmp_path: Path) -> None:
    project = _stable_then(tmp_path, VOLUME, day16_rows=100)
    _assert_round_trip(project, FAILURES[0][2])


def test_freshness_anomaly_audit_transpiles(tmp_path: Path) -> None:
    project = _project(tmp_path, FRESHNESS)
    project.add_days(1, 5, rows=10)
    project.plan(execution_time="2024-01-06")
    project.run(execution_time="2024-01-09")
    _assert_round_trip(project, FAILURES[0][2])


@pytest.mark.parametrize("dialect", ["snowflake", "bigquery"])
def test_audits_render_in_other_project_dialects(tmp_path: Path, dialect: str) -> None:
    """The audit text parses in the project's dialect. Render it as a Snowflake or BigQuery
    project would; the result parses back and names the model with that dialect's normalization."""
    import sqlglot
    from sqlglot import exp
    from sqlmesh.core.config import (
        Config,
        DuckDBConnectionConfig,
        GatewayConfig,
        ModelDefaultsConfig,
    )
    from sqlmesh.core.context import Context

    from sqlmesh_observability.audits import copy_audits

    root = tmp_path / "project"
    (root / "models").mkdir(parents=True)
    (root / "models" / "daily.sql").write_text(
        MODEL_SQL.replace(
            "__AUDITS__",
            "volume_anomaly(time_column := ds, source := obs.daily, "
            "spike_failure_percent_threshold := 50, drop_failure_percent_threshold := 20), "
            "column_anomaly(column := id, metric := null_rate, time_column := ds, "
            "source := obs.daily), " + FRESHNESS,
        ).replace("__EXTRA__", "")
    )
    copy_audits(root)
    config = Config(
        gateways={"local": GatewayConfig(connection=DuckDBConnectionConfig())},
        default_gateway="local",
        model_defaults=ModelDefaultsConfig(dialect=dialect),
    )
    context = Context(paths=str(root), config=config, load=True)
    model = context.get_model("obs.daily")
    expected = exp.to_table(model.fqn, dialect=dialect).name

    rendered = {
        audit.name: model.render_audit_query(
            audit, start="2024-01-16", end="2024-01-16", execution_time="2024-01-17", **args
        )
        for audit, args in model.audits_with_args
    }
    assert set(rendered) == {"volume_anomaly", "column_anomaly", "freshness_anomaly"}
    for query in rendered.values():
        sql = query.sql(dialect=dialect, unsupported_level=sqlglot.ErrorLevel.RAISE)
        sqlglot.parse_one(sql, read=dialect)
        assert expected in {table.name for table in query.find_all(exp.Table)}


# --- init-audits ----------------------------------------------------------------------------------


def test_init_audits_copies_and_protects_edits(tmp_path: Path) -> None:
    from importlib import resources

    from sqlmesh_observability.cli import main

    shipped = resources.files("sqlmesh_observability.audits")
    names = sorted(p.name for p in shipped.iterdir() if p.name.endswith(".sql"))
    assert names == ["column_anomaly.sql", "freshness_anomaly.sql", "volume_anomaly.sql"]

    assert main(["init-audits", "--path", str(tmp_path)]) == 0
    for name in names:
        assert (tmp_path / "audits" / name).read_text() == (shipped / name).read_text()
    macros = tmp_path / "macros" / "observability_audits.py"
    assert macros.read_text() == (shipped / "macros.py").read_text()

    edited = tmp_path / "audits" / "volume_anomaly.sql"
    edited.write_text("-- my edit\n")
    assert main(["init-audits", "--path", str(tmp_path)]) != 0
    assert edited.read_text() == "-- my edit\n"

    assert main(["init-audits", "--path", str(tmp_path), "--force"]) == 0
    assert edited.read_text() == (shipped / "volume_anomaly.sql").read_text()
