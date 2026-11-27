"""The package's own tables keep one spelling on case-sensitive engines (Snowflake).

SQLMesh's adapters quote names when they create, insert, or delete, and leave them unquoted when
they fetch. On Snowflake, ``CREATE TABLE "observability"."invocations"`` and
``SELECT ... FROM observability.invocations`` (which resolves to ``OBSERVABILITY.INVOCATIONS``)
are different tables, so every name the package writes must already be in the dialect's case.
These tests run on a Snowflake adapter over a mocked connection and check the SQL it would send.
"""

from __future__ import annotations

import datetime
import typing as t
from unittest import mock

import pytest

pytest.importorskip("snowflake.connector")

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

NOW = datetime.datetime(2024, 1, 2, tzinfo=datetime.timezone.utc)


def _adapter() -> t.Tuple[t.Any, t.List[str]]:
    from sqlmesh.core.engine_adapter.snowflake import SnowflakeEngineAdapter

    executed: t.List[str] = []
    cursor = mock.Mock()
    cursor.execute.side_effect = lambda sql, *args, **kwargs: executed.append(sql)
    cursor.fetchone.return_value = None
    cursor.fetchall.return_value = []
    connection = mock.NonCallableMock()
    connection.cursor.return_value = cursor
    adapter = SnowflakeEngineAdapter(lambda *a, **k: connection, dialect="snowflake")
    return adapter, executed


def _lower_case_names(statements: t.Iterable[str]) -> t.List[str]:
    found = []
    for sql in statements:
        for expression in sqlglot.parse(sql, read="snowflake"):
            if expression is None:
                continue
            for identifier in expression.find_all(exp.Identifier):
                if identifier.quoted and identifier.name != identifier.name.upper():
                    found.append(f"{identifier.name!r} in {sql[:120]}")
    return found


def _invocation() -> t.Any:
    from sqlmesh_observability.collector import AuditRun, Invocation, ModelRun, SchemaCapture

    return Invocation(
        invocation_id="inv-1",
        command="plan",
        environment="prod",
        plan_id="plan-1",
        started_at=NOW,
        finished_at=NOW,
        status="success",
        models=[
            ModelRun(
                invocation_id="inv-1",
                model_name='"DB"."OBS"."EVENTS"',
                snapshot_id="s1",
                version="v1",
                interval_start=0,
                interval_end=86_400_000,
                batch_index=0,
                execution_time=0,
                audit_only=False,
                environment="prod",
                started_at=NOW,
                finished_at=NOW,
                status="success",
            )
        ],
        audits=[
            AuditRun(
                invocation_id="inv-1",
                audit_name="not_null",
                model_name='"DB"."OBS"."EVENTS"',
                snapshot_id="s1",
                version="v1",
                physical_table='"DB"."sqlmesh__OBS"."OBS__EVENTS__1"',
                interval_start=0,
                interval_end=86_400_000,
                environment="prod",
                finished_at=NOW,
            )
        ],
        schemas=[
            SchemaCapture(
                invocation_id="inv-1",
                model_name='"DB"."OBS"."EVENTS"',
                snapshot_id="s1",
                version="v1",
                action="create",
                previous_snapshot_ids=(),
                columns=(("ID", "INT"),),
                recorded_at=NOW,
            )
        ],
    )


def test_writer_names_are_in_the_dialect_case() -> None:
    from sqlmesh_observability.monitors import MonitorSettings
    from sqlmesh_observability.writer import Writer

    adapter, executed = _adapter()
    writer = Writer(mock.Mock(), "sf", monitors=MonitorSettings(schema=True))
    with mock.patch.object(Writer, "_adapter", return_value=adapter):
        writer.write(_invocation())

    assert any("INVOCATIONS" in sql for sql in executed)
    assert _lower_case_names(executed) == []


def test_watermark_names_are_in_the_dialect_case() -> None:
    from sqlmesh_observability.alerts import read_watermark, write_watermark
    from sqlmesh_observability.logtables import LogTables

    adapter, executed = _adapter()
    log = LogTables(adapter)
    write_watermark(log, "observability", "prod", (NOW.replace(tzinfo=None), "inv-1"))
    read_watermark(log, "observability", "prod")

    assert any("OBSERVABILITY" in sql for sql in executed)
    assert _lower_case_names(executed) == []


def test_reader_names_are_in_the_dialect_case() -> None:
    from sqlmesh_observability import reader

    adapter, executed = _adapter()
    config = mock.Mock()
    config.get_connection.return_value.create_engine_adapter.return_value = adapter
    with (
        mock.patch.object(
            reader, "_resolve_gateway_and_schema", return_value=("sf", "observability")
        ),
        mock.patch.object(reader, "_read_environment_state", return_value=None),
    ):
        reader.load_report_data_for(config, {}, environment="prod")

    reads = [sql for sql in executed if sql.lstrip().upper().startswith("SELECT")]
    assert reads and all("OBSERVABILITY" in sql.upper() for sql in reads)
    assert _lower_case_names(executed) == []
    # The reads resolve to the tables the writer creates: upper-case names, quoted or not.
    assert not any('"observability"' in sql or "observability." in sql for sql in executed)


def test_monitor_queries_keep_the_physical_names() -> None:
    """Model tables keep SQLMesh's spelling: its physical schema is a quoted ``sqlmesh__...``."""
    from sqlmesh_observability.monitors import MonitorSettings, ScoreSettings, collect_metrics

    adapter, executed = _adapter()
    invocation = _invocation()
    run = invocation.models[0]
    # The collector stores the physical table as SQLMesh renders it, without quotes.
    run.physical_table = "DB.sqlmesh__OBS.OBS__EVENTS__1"
    run.time_column = "ds"  # as written in the model; the table reports "DS"
    run.columns = (("DS", "DATE"), ("ID", "INT"))
    describe = {"DS": exp.DataType.build("DATE"), "ID": exp.DataType.build("INT")}
    with mock.patch.object(adapter, "columns", return_value=describe):
        rows = collect_metrics(
            invocation, adapter, adapter, MonitorSettings(volume=ScoreSettings())
        )

    assert [r.get("error_message") for r in rows] == [None]
    (count,) = [sql for sql in executed if "COUNT(" in sql.upper()]
    assert '"sqlmesh__OBS"' in count and '"DS"' in count
