"""Tests: ``sqlmesh-observe monitor`` grouped alerts."""

from __future__ import annotations

import typing as t
from pathlib import Path

import pytest
from conftest import RUN_EXECUTION_TIME, Probe
from test_report import (  # noqa: F401  (collector is a fixture)
    FIVE_NULL_ROWS,
    ONE_ROW,
    _install_on_load,
    _plan,
    _write_model,
    collector,
)

SCHEMA = "observability"

SENT: t.List[t.Tuple[t.Any, str, t.Dict[str, t.Any]]] = []


@pytest.fixture(autouse=True)
def clear_sent() -> t.Iterator[None]:
    SENT.clear()
    yield
    SENT.clear()


def _target(explode: bool = False) -> t.Any:
    from sqlmesh.core.notification_target import GenericNotificationTarget

    class Recorder(GenericNotificationTarget):
        def send(self, notification_status: t.Any, msg: str, **kwargs: t.Any) -> None:
            if explode:
                raise RuntimeError("chat service is down")
            SENT.append((notification_status, msg, kwargs))

    return Recorder()


def _monitor(project: Path) -> int:
    from sqlmesh_observability import cli

    return cli.main(["monitor", "--path", str(project)])


def _watermarks(project: Path) -> t.List[t.Tuple[t.Any, ...]]:
    import duckdb

    connection = duckdb.connect(str(project.parent / "warehouse.duckdb"), read_only=True)
    try:
        exists = connection.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            f"WHERE table_schema = '{SCHEMA}' AND table_name = 'alert_watermarks'"
        ).fetchone()[0]
        if not exists:
            return []
        return connection.execute(
            f"SELECT environment, watermark_at, invocation_id, updated_at "
            f"FROM {SCHEMA}.alert_watermarks"
        ).fetchall()
    finally:
        connection.close()


NULL_ID = "SELECT CAST(@start_ds AS DATE) AS ds, CAST(NULL AS INT) AS id"
BAD_ID = "SELECT CAST(@start_ds AS DATE) AS ds, 999 AS id"


def _owned_failures(project: Path, probe: Probe, **install: t.Any) -> None:
    """Prod: two non-blocking failures owned by payments-team, one by growth-team, one pass."""
    from sqlmesh.core.context import Context

    _install_on_load(probe, **install)
    _write_model(
        project, "events.sql", "obs.events", ONE_ROW, "not_null(columns := (id))", owner="ops"
    )
    _write_model(
        project,
        "payments.sql",
        "obs.payments",
        NULL_ID,
        "not_null_non_blocking(columns := (id))",
        owner="payments-team",
    )
    _write_model(
        project,
        "refunds.sql",
        "obs.refunds",
        BAD_ID,
        "accepted_values_non_blocking(column := id, is_in := (1, 2))",
        owner="payments-team",
    )
    _write_model(
        project,
        "signups.sql",
        "obs.signups",
        NULL_ID,
        "not_null_non_blocking(columns := (id))",
        owner="growth-team",
    )
    context = Context(paths=str(project))
    _plan(context)
    context.close()


@pytest.mark.usefixtures("collector")
def test_monitor_groups_by_owner(project: Path, probe: Probe) -> None:
    from sqlmesh.core.notification_target import NotificationStatus

    _owned_failures(project, probe, alert_target=_target())
    assert SENT == [], "the alert target is never called during a plan"
    assert _monitor(project) == 0

    by_owner = {kwargs["owner"]: (status, msg, kwargs) for status, msg, kwargs in SENT}
    assert len(SENT) == 2 and set(by_owner) == {"payments-team", "growth-team"}
    status, msg, kwargs = by_owner["payments-team"]
    assert "not_null_non_blocking" in msg and "accepted_values_non_blocking" in msg
    assert "obs.payments" in msg and "obs.refunds" in msg
    assert "signups" not in msg and "obs.events" not in msg
    assert status == NotificationStatus.WARNING
    assert {a["model_name"].replace('"', "").split(".")[-1] for a in kwargs["alerts"]} == {
        "payments",
        "refunds",
    }
    assert all(isinstance(a, dict) for a in kwargs["alerts"])


@pytest.mark.usefixtures("collector")
def test_monitor_watermark_advances_only_after_send(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context

    _owned_failures(project, probe, alert_target=_target())
    assert _watermarks(project) == []
    assert _monitor(project) == 0
    ((environment, watermark_at, invocation_id, updated_at),) = _watermarks(project)
    assert environment == "prod" and watermark_at is not None and invocation_id

    SENT.clear()
    assert _monitor(project) == 0
    assert SENT == []
    assert _watermarks(project) == [(environment, watermark_at, invocation_id, updated_at)]

    # A later run fails the new intervals again: only those are sent.
    context = Context(paths=str(project))
    context.run(execution_time=RUN_EXECUTION_TIME)
    context.close()
    assert _monitor(project) == 0
    messages = "\n".join(msg for _, msg, _ in SENT)
    assert "2024-01-03" in messages and "2024-01-01" not in messages
    ((_, newer, newer_id, _),) = _watermarks(project)
    assert newer > watermark_at and newer_id != invocation_id


@pytest.mark.usefixtures("collector")
def test_monitor_retries_when_send_raises(
    project: Path, probe: Probe, capsys: pytest.CaptureFixture
) -> None:
    _owned_failures(project, probe, alert_target=_target(explode=True))
    assert _monitor(project) == 1
    assert "chat service is down" in capsys.readouterr().err
    assert _watermarks(project) == []

    _install_on_load(probe, alert_target=_target())
    assert _monitor(project) == 0
    assert {kwargs["owner"] for _, _, kwargs in SENT} == {"payments-team", "growth-team"}
    assert len(_watermarks(project)) == 1


@pytest.mark.usefixtures("collector")
def test_monitor_ignores_dev(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context

    _install_on_load(probe, alert_target=_target())
    _write_model(project, "events.sql", "obs.events", ONE_ROW, "not_null(columns := (id))")
    _write_model(
        project,
        "refunds.sql",
        "obs.refunds",
        BAD_ID,
        "accepted_values_non_blocking(column := id, is_in := (1, 2))",
    )
    context = Context(paths=str(project))
    _plan(context)
    # A dev-only version of obs.events whose not_null fails; it is never promoted.
    _write_model(
        project, "events.sql", "obs.events", NULL_ID, "not_null_non_blocking(columns := (id))"
    )
    context.load()
    _plan(context, "dev", start="2024-01-01", end="2024-01-02")
    context.close()

    assert _monitor(project) == 0
    ((_, msg, kwargs),) = SENT
    assert kwargs["owner"] is None
    assert "accepted_values_non_blocking" in msg
    assert "not_null" not in msg


@pytest.mark.usefixtures("collector")
def test_monitor_prints_groups_without_a_target(
    project: Path, probe: Probe, capsys: pytest.CaptureFixture
) -> None:
    _owned_failures(project, probe)
    assert _monitor(project) == 0
    out = capsys.readouterr().out
    assert "for payments-team" in out and "for growth-team" in out
    assert "not_null_non_blocking" in out
    assert len(_watermarks(project)) == 1


@pytest.mark.usefixtures("collector")
def test_monitor_sends_blocking_failures_as_failure(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context
    from sqlmesh.core.notification_target import NotificationStatus
    from sqlmesh.utils.errors import PlanError

    _install_on_load(probe, alert_target=_target())
    _write_model(project, "events.sql", "obs.events", ONE_ROW, "not_null(columns := (id))")
    context = Context(paths=str(project))
    _plan(context)
    _write_model(
        project,
        "broken.sql",
        "obs.broken",
        FIVE_NULL_ROWS,
        "not_null(columns := (id))",
        owner="payments-team",
    )
    context.load()
    with pytest.raises(PlanError):
        _plan(context)
    context.close()

    assert _monitor(project) == 0
    ((status, msg, _),) = SENT
    assert status == NotificationStatus.FAILURE
    assert "blocking failure" in msg and "5 rows" in msg


@pytest.mark.usefixtures("collector")
def test_monitor_alerts_anomalies(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context

    _install_on_load(probe, monitors={"volume": True}, alert_target=_target())
    _write_model(
        project,
        "events.sql",
        "obs.events",
        "SELECT CAST(@start_ds AS DATE) AS ds, id FROM raw.src",
        "not_null(columns := (id))",
        owner="ops",
    )
    context = Context(paths=str(project))
    context.engine_adapter.execute("CREATE SCHEMA IF NOT EXISTS raw")
    context.engine_adapter.execute("CREATE TABLE raw.src AS SELECT range AS id FROM range(10)")
    context.load()
    _plan(context, execution_time="2024-01-09")
    context.engine_adapter.execute("INSERT INTO raw.src SELECT range FROM range(10, 500)")
    context.run(execution_time="2024-01-10")
    context.close()

    assert _monitor(project) == 0
    ((_, msg, kwargs),) = SENT
    assert kwargs["owner"] == "ops"
    assert "row_count anomaly" in msg and "2024-01-09" in msg and "500" in msg


def test_install_records_alert_target_without_adding_it() -> None:
    from sqlmesh.core.config import Config

    from sqlmesh_observability import install
    from sqlmesh_observability.collector import _reset_for_tests, get_collector

    _reset_for_tests()
    try:
        target = _target()
        config = install(Config(), "local", alert_target=target)
        assert get_collector().settings["alert_target"] is target
        assert target not in config.notification_targets
    finally:
        _reset_for_tests()
