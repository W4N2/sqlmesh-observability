"""Tests: the read-only ``serve`` app.

Response bodies are checked
with stdlib ``html.parser``; no browser.
"""

from __future__ import annotations

import sys
import typing as t
from pathlib import Path

import pytest
from conftest import Probe
from test_report import (  # noqa: F401  (collector is a fixture)
    FIVE_NULL_ROWS,
    ONE_ROW,
    _install_on_load,
    _plan,
    _write_model,
    collector,
)
from test_report_data import decode
from test_report_ui import _Elements

if t.TYPE_CHECKING:
    from starlette.testclient import TestClient

SCHEMA = "observability"


def _client(project: Path, **kwargs: t.Any) -> "TestClient":
    from starlette.testclient import TestClient

    from sqlmesh_observability.app import create_app

    return TestClient(create_app(project, **kwargs))


def _failing_prod(project: Path, probe: Probe) -> None:
    """Prod has a passing ``obs.events`` and a blocking ``not_null`` failure on ``obs.broken``."""
    from sqlmesh.core.context import Context
    from sqlmesh.utils.errors import PlanError

    _install_on_load(probe, sample_rows=2)
    _write_model(project, "events.sql", "obs.events", ONE_ROW, "not_null(columns := (id))")
    context = Context(paths=str(project))
    _plan(context)
    _write_model(project, "broken.sql", "obs.broken", FIVE_NULL_ROWS, "not_null(columns := (id))")
    context.load()
    with pytest.raises(PlanError):
        _plan(context)
    context.close()


def _counts(project: Path) -> t.Tuple[int, int]:
    import duckdb

    connection = duckdb.connect(str(project.parent / "warehouse.duckdb"), read_only=True)
    try:
        return tuple(  # type: ignore[return-value]
            connection.execute(f"SELECT COUNT(*) FROM {SCHEMA}.{table}").fetchone()[0]
            for table in ("audit_results", "invocations")
        )
    finally:
        connection.close()


def _body_start(document: str) -> str:
    (body,) = _Elements(document).find("body")
    return body.attrs.get("data-start", "")


@pytest.mark.usefixtures("collector")
def test_overview_returns_failing_audit(project: Path, probe: Probe) -> None:
    _failing_prod(project, probe)
    response = _client(project).get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "not_null" in response.text
    assert "obs.broken" in response.text
    assert _body_start(response.text) == ""


@pytest.mark.usefixtures("collector")
def test_pass_only_model_route_opens_the_model(project: Path, probe: Probe) -> None:
    from sqlmesh_observability.report import model_route

    _failing_prod(project, probe)
    client = _client(project)
    response = client.get("/models/obs.events")
    assert response.status_code == 200

    payload = decode(response.text)
    (events,) = [m for m in payload["models"] if m["n"].replace('"', "").endswith(".events")]
    assert _body_start(response.text) == model_route(events["n"])
    assert events["st"] == "pass"

    # The full quoted name and the unquoted name resolve to the same page.
    quoted = client.get("/models/" + events["n"])
    assert quoted.status_code == 200 and _body_start(quoted.text) == model_route(events["n"])


@pytest.mark.usefixtures("collector")
def test_incident_route_opens_the_incident(project: Path, probe: Probe) -> None:
    from sqlmesh_observability.reader import load_report_data
    from sqlmesh_observability.report import incident_route

    _failing_prod(project, probe)
    incident = load_report_data(project).incidents[0]
    response = _client(project).get(f"/incidents/{incident.invocation_id}/{incident.audit_name}")
    assert response.status_code == 200
    payload = decode(response.text)
    start = _body_start(response.text)
    assert start.startswith("incidents?i=")
    opened = payload["incidents"][int(start.split("=")[1])]
    assert start == incident_route(int(start.split("=")[1]))
    assert opened["invocation"] == incident.invocation_id
    assert payload["audits"][opened["use"]][0] == incident.audit_name
    assert opened["samples"]


@pytest.mark.usefixtures("collector")
def test_data_endpoint_matches_the_embedded_payload(project: Path, probe: Probe) -> None:
    _failing_prod(project, probe)
    client = _client(project)
    response = client.get("/data.json")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    page = decode(client.get("/").text)
    data = response.json()
    data.pop("generated_at"), page.pop("generated_at")
    assert data == page
    assert client.post("/data.json").status_code == 405


@pytest.mark.usefixtures("collector")
def test_unknown_model_and_incident_are_404(project: Path, probe: Probe) -> None:
    _failing_prod(project, probe)
    client = _client(project)
    assert client.get("/models/obs.nope").status_code == 404
    assert client.get("/incidents/no-such-invocation/not_null").status_code == 404


@pytest.mark.usefixtures("collector")
def test_serve_does_not_write(project: Path, probe: Probe) -> None:
    _failing_prod(project, probe)
    before = _counts(project)
    client = _client(project)
    assert client.get("/").status_code == 200
    assert client.post("/").status_code == 405
    assert client.post("/models/obs.events").status_code == 405
    assert _counts(project) == before


@pytest.mark.usefixtures("collector")
def test_missing_audit_table_is_503(project: Path, probe: Probe) -> None:
    _install_on_load(probe)
    response = _client(project).get("/")
    assert response.status_code == 503
    assert "audit_results" in response.text


@pytest.mark.usefixtures("collector")
def test_overview_shows_schema_change_and_column_anomaly(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context

    _install_on_load(
        probe, monitors={"schema": True, "columns": {"events": {"null_rate": ["status"]}}}
    )
    select = "SELECT CAST(@start_ds AS DATE) AS ds, id, status FROM raw.src"
    _write_model(project, "events.sql", "obs.events", select, "not_null(columns := (id))")
    context = Context(paths=str(project))
    context.engine_adapter.execute("CREATE SCHEMA IF NOT EXISTS raw")
    context.engine_adapter.execute(
        "CREATE TABLE raw.src AS SELECT range AS id, 'ok' AS status FROM range(10)"
    )
    context.load()
    _plan(context, execution_time="2024-01-09")  # 2024-01-01 .. 08, no nulls
    context.engine_adapter.execute("INSERT INTO raw.src SELECT range, NULL FROM range(10, 500)")
    context.run(execution_time="2024-01-10")  # 2024-01-09 is 98% null
    _write_model(
        project,
        "events.sql",
        "obs.events",
        select.replace("status", "status, 1 AS added_col"),
        "not_null(columns := (id))",
    )
    context.load()
    _plan(context, forward_only=True, execution_time="2024-01-10")
    context.close()

    response = _client(project).get("/")
    assert response.status_code == 200
    assert "added_col" in response.text
    assert "null_rate (status)" in response.text
    assert 'id="overview-anomalies"' in response.text
    payload = decode(response.text)
    assert any(x[0] == "added_col" for m in payload["models"] for x in m["x"])


def test_serve_without_starlette_prints_the_extra(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    from sqlmesh_observability import cli

    monkeypatch.setitem(sys.modules, "starlette", None)
    monkeypatch.setitem(sys.modules, "starlette.applications", None)
    assert cli.main(["serve", "--path", str(tmp_path)]) == 1
    assert "sqlmesh-observability[serve]" in capsys.readouterr().err


def test_serve_runs_uvicorn_with_the_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import uvicorn

    from sqlmesh_observability import cli

    calls: t.List[t.Tuple[t.Any, t.Dict[str, t.Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    assert cli.main(["serve", "--path", str(tmp_path), "--port", "8123"]) == 0
    ((app, kwargs),) = calls
    assert kwargs["host"] == "127.0.0.1" and kwargs["port"] == 8123
    assert callable(app)


@pytest.mark.usefixtures("collector")
def test_static_report_has_no_start_view(project: Path, probe: Probe) -> None:
    from sqlmesh_observability.reader import load_report_data
    from sqlmesh_observability.report import model_route, render_report

    _failing_prod(project, probe)
    data = load_report_data(project)
    assert "data-start" not in _Elements(render_report(data)).find("body")[0].attrs
    anchor = model_route(data.models[0].model_name)
    assert _body_start(render_report(data, start=anchor)) == anchor
