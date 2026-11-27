"""End to end on Snowflake: the log writer, monitors, package audits, reader, report, and monitor.

Everything else in the suite runs on DuckDB. This test runs one short history on a real Snowflake
account. It is skipped unless ``SNOWFLAKE_ACCOUNT`` is set (``REQUIRE_SNOWFLAKE_TESTS=1`` makes a
skip a failure). Connection settings come from the environment and are only read when SQLMesh
loads the generated ``config.py``; nothing secret is written to disk:

    SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_WAREHOUSE, SNOWFLAKE_DATABASE   required
    SNOWFLAKE_ROLE                                                               optional
    SNOWFLAKE_PASSWORD, or SNOWFLAKE_PRIVATE_KEY (PEM text) / SNOWFLAKE_PRIVATE_KEY_PATH
    with SNOWFLAKE_PRIVATE_KEY_PASSPHRASE when the key is encrypted

Use a scratch database. The test creates schemas with a random suffix (``OBS_<suffix>``,
``SQLMESH__OBS_<suffix>``, ``RAW_<suffix>``, ``OBSERVABILITY_<suffix>``) and drops them at the end.
SQLMesh state stays in a local DuckDB file.
"""

from __future__ import annotations

import json
import os
import typing as t
import uuid
from pathlib import Path

import pytest
from test_report import collector  # noqa: F401  (a fixture)

REQUIRED = os.environ.get("REQUIRE_SNOWFLAKE_TESTS") == "1"
NEEDED = ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_DATABASE")


def _unavailable(reason: str) -> None:
    if REQUIRED:
        pytest.fail(f"Snowflake tests are required here, but {reason}")
    pytest.skip(reason, allow_module_level=True)


_missing = [name for name in NEEDED if not os.environ.get(name)]
if _missing:
    _unavailable(f"set {', '.join(_missing)} to run the Snowflake test")
try:
    import snowflake.connector  # noqa: F401
except ImportError:
    _unavailable("the Snowflake connector is missing: pip install 'sqlmesh[snowflake]'")

pytestmark = pytest.mark.snowflake

CONFIG_PY = """
import os
from sqlmesh.core.config import (
    Config, DuckDBConnectionConfig, GatewayConfig, ModelDefaultsConfig, SnowflakeConnectionConfig,
)
from sqlmesh_observability import install

settings = {
    "account": os.environ["SNOWFLAKE_ACCOUNT"],
    "user": os.environ["SNOWFLAKE_USER"],
    "warehouse": os.environ["SNOWFLAKE_WAREHOUSE"],
    "database": os.environ["SNOWFLAKE_DATABASE"],
    "role": os.environ.get("SNOWFLAKE_ROLE"),
    "password": os.environ.get("SNOWFLAKE_PASSWORD"),
    "private_key": os.environ.get("SNOWFLAKE_PRIVATE_KEY"),
    "private_key_path": os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH"),
    "private_key_passphrase": os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"),
}
config = install(
    Config(
        gateways={
            "sf": GatewayConfig(
                connection=SnowflakeConnectionConfig(**{k: v for k, v in settings.items() if v}),
                state_connection=DuckDBConnectionConfig(database=__STATE__),
            )
        },
        default_gateway="sf",
        model_defaults=ModelDefaultsConfig(dialect="snowflake", start="2024-01-01"),
    ),
    "sf",
    schema=__OBS_SCHEMA__,
    monitors={"volume": True, "schema": True},
    sample_rows=2,
)
"""

EVENTS = """
MODEL (
  name __OBS__.events,
  owner 'data-eng',
  kind INCREMENTAL_BY_TIME_RANGE (time_column ds, batch_size 1),
  cron '@daily',
  audits (
    not_null(columns := (id)),
    volume_anomaly(time_column := ds, source := __OBS__.events)
  )
);

SELECT ds, id, status__EXTRA__ FROM __RAW__.src WHERE ds BETWEEN @start_ds AND @end_ds
"""

BROKEN = """
MODEL (
  name __OBS__.broken,
  owner 'payments',
  kind INCREMENTAL_BY_TIME_RANGE (time_column ds, batch_size 1),
  cron '@daily',
  audits (not_null(columns := (id)))
);

SELECT ds, CAST(NULL AS INT) AS id FROM __OBS__.events WHERE ds BETWEEN @start_ds AND @end_ds
"""


def _plan(context: t.Any, **kwargs: t.Any) -> None:
    context.plan("prod", auto_apply=True, no_prompts=True, **kwargs)


def _add_days(context: t.Any, raw: str, first: int, last: int, rows: int) -> None:
    for day in range(first, last + 1):
        context.engine_adapter.execute(
            f"INSERT INTO {raw}.src SELECT DATE '2024-01-{day:02d}', SEQ4(), 'ok' "
            f"FROM TABLE(GENERATOR(ROWCOUNT => {rows}))"
        )


@pytest.mark.usefixtures("collector")
def test_snowflake_end_to_end(tmp_path: Path) -> None:
    from sqlmesh.core.context import Context
    from sqlmesh.utils.errors import PlanError
    from test_report_data import decode

    from sqlmesh_observability.alerts import run_monitor
    from sqlmesh_observability.audits import copy_audits
    from sqlmesh_observability.reader import load_report_data
    from sqlmesh_observability.report import render_report

    suffix = uuid.uuid4().hex[:8].upper()
    obs, raw, log = f"OBS_{suffix}", f"RAW_{suffix}", f"OBSERVABILITY_{suffix}"
    root = tmp_path / "project"
    (root / "models").mkdir(parents=True)
    (root / "config.py").write_text(
        CONFIG_PY.replace("__STATE__", repr(str(tmp_path / "state.duckdb"))).replace(
            "__OBS_SCHEMA__", repr(log)
        )
    )
    copy_audits(root)

    def write(name: str, body: str, extra: str = "") -> None:
        text = body.replace("__OBS__", obs).replace("__RAW__", raw).replace("__EXTRA__", extra)
        (root / "models" / f"{name}.sql").write_text(text)

    write("events", EVENTS)
    context = Context(paths=str(root))
    adapter = context.engine_adapter
    try:
        adapter.execute(f"CREATE SCHEMA {raw}")
        adapter.execute(f"CREATE TABLE {raw}.src (ds DATE, id INT, status VARCHAR)")
        _add_days(context, raw, 1, 14, rows=10)

        # Days 1-14, then a run on day 15 with 50x the rows: volume_anomaly fails (non-blocking).
        _plan(context, execution_time="2024-01-15")
        _add_days(context, raw, 15, 15, rows=500)
        context.run(execution_time="2024-01-16")

        # A forward-only change adds a column (schema monitor); a new model fails its blocking
        # audit, so prod is not promoted.
        write("events", EVENTS, extra=", 1 AS added_col")
        context.load()
        _plan(context, forward_only=True, execution_time="2024-01-16")
        write("broken", BROKEN)
        context.load()
        with pytest.raises(PlanError):
            _plan(context, execution_time="2024-01-16")

        data = load_report_data(root)
        by_leaf = {m.model_name.replace('"', "").split(".")[-1].lower(): m for m in data.models}
        events, broken = by_leaf["events"], by_leaf["broken"]

        assert events.owner == "data-eng" and broken.owner == "payments"
        assert {name.lower() for name, _ in events.columns} >= {"ds", "id", "status"}
        assert {a.audit_name for a in events.audits if a.status == "pass"} >= {"not_null"}
        volume = [a for a in events.audits if a.audit_name == "volume_anomaly"]
        assert [(a.interval_start or "")[:10] for a in volume if a.status == "fail"] == [
            "2024-01-15"
        ]
        assert all(a.blocking is False for a in volume)

        (series,) = [s for s in events.metrics if s.metric_name == "row_count"]
        assert series.points[-1].value == 500 and series.points[-1].anomaly
        assert [(c.column_name.lower(), c.change_type) for c in events.schema_changes] == [
            ("added_col", "added")
        ]

        (failure,) = broken.failing_blocking_audits
        assert failure.audit_name == "not_null" and failure.violation_count
        assert len(failure.samples) == 2
        for row in map(json.loads, failure.samples):  # Snowflake upper-cases unquoted names
            assert row.get("ID", row.get("id")) is None

        edges = {
            (p.split(".")[-1].strip('"').lower(), n.model_name.split(".")[-1].strip('"').lower())
            for n in data.lineage
            for p in n.parents
        }
        assert ("events", "broken") in edges

        payload = decode(render_report(data))
        assert payload["summary"]["models"]["fail"] == 1
        assert {i["kind"] for i in payload["issues"]} >= {"blocking", "anomaly", "warning"}

        assert run_monitor(path=root) == 0
    finally:
        for schema in (obs, f"SQLMESH__{obs}", raw, log):
            try:
                adapter.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            except Exception:
                pass
        context.close()
