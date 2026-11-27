"""Tests: model lineage read from SQLMesh state, and how the report carries it."""

from __future__ import annotations

import typing as t
from pathlib import Path

import pytest
from conftest import Probe
from test_report import (  # noqa: F401  (collector is a fixture)
    FIVE_NULL_ROWS,
    _install_on_load,
    _plan,
    _write_model,
    collector,
)

if t.TYPE_CHECKING:
    from sqlmesh_observability.reader import ReportData

EXTERNAL_MODELS = """
- name: raw.orders
  columns:
    ds: date
    id: int
"""

ORDERS = "SELECT CAST(@start_ds AS DATE) AS ds, id FROM raw.orders"
DAILY = "SELECT CAST(@start_ds AS DATE) AS ds, COUNT(*) AS id FROM obs.orders"
LTV = "SELECT CAST(@start_ds AS DATE) AS ds, MAX(id) AS id FROM obs.orders"


def _key(name: str) -> str:
    """``"db"."obs"."orders"`` -> ``obs.orders``."""
    return ".".join(name.replace('"', "").split(".")[-2:])


def _edges(data: "ReportData") -> t.Set[t.Tuple[str, str]]:
    return {(_key(p), _key(n.model_name)) for n in data.lineage for p in n.parents}


def _chain(project: Path) -> t.Any:
    from sqlmesh.core.context import Context

    (project / "external_models.yaml").write_text(EXTERNAL_MODELS)
    _write_model(project, "orders.sql", "obs.orders", ORDERS, "not_null(columns := (id))")
    _write_model(project, "daily.sql", "obs.daily_revenue", DAILY, "not_null(columns := (id))")
    _write_model(project, "ltv.sql", "obs.customer_ltv", LTV, "not_null(columns := (id))")
    context = Context(paths=str(project))
    context.engine_adapter.execute("CREATE SCHEMA IF NOT EXISTS raw")
    context.engine_adapter.execute(
        "CREATE TABLE raw.orders AS SELECT DATE '2024-01-01' AS ds, 1 AS id"
    )
    context.load()
    return context


@pytest.mark.usefixtures("collector")
def test_lineage_comes_from_state(project: Path, probe: Probe) -> None:
    from sqlmesh_observability.reader import load_report_data

    _install_on_load(probe)
    (project / "models" / "events.sql").unlink()
    context = _chain(project)
    _plan(context)
    context.close()

    data = load_report_data(project)
    assert _edges(data) == {
        ("raw.orders", "obs.orders"),
        ("obs.orders", "obs.daily_revenue"),
        ("obs.orders", "obs.customer_ltv"),
    }
    kinds = {_key(n.model_name): n.kind for n in data.lineage}
    assert kinds["raw.orders"] == "EXTERNAL"
    assert kinds["obs.orders"] == "INCREMENTAL_BY_TIME_RANGE"


@pytest.mark.usefixtures("collector")
def test_lineage_includes_a_blocked_new_model(project: Path, probe: Probe) -> None:
    from sqlmesh.utils.errors import PlanError

    from sqlmesh_observability.reader import load_report_data

    _install_on_load(probe)
    (project / "models" / "events.sql").unlink()
    context = _chain(project)
    _plan(context)
    _write_model(
        project,
        "refunds.sql",
        "obs.refunds",
        "SELECT CAST(@start_ds AS DATE) AS ds, CAST(NULL AS INT) AS id FROM obs.orders",
        "not_null(columns := (id))",
    )
    context.load()
    with pytest.raises(PlanError):
        _plan(context)
    context.close()

    data = load_report_data(project)
    assert ("obs.orders", "obs.refunds") in _edges(data)
    assert data.failing_model_count == 1


@pytest.mark.usefixtures("collector")
def test_external_model_without_rows_is_a_lineage_only_node(project: Path, probe: Probe) -> None:
    from test_report_data import decode

    from sqlmesh_observability.reader import load_report_data
    from sqlmesh_observability.report import render_report

    _install_on_load(probe)
    (project / "models" / "events.sql").unlink()
    context = _chain(project)
    _plan(context)
    context.close()

    payload = decode(render_report(load_report_data(project)))
    lineage = payload["lineage"]
    assert [_key(name) for name in lineage["extra"]] == ["raw.orders"]
    assert sum(payload["summary"]["models"].values()) == 3
    raw = len(payload["models"])  # the first lineage-only node
    assert lineage["kinds"][lineage["kind"][raw]] == "EXTERNAL"
    assert lineage["impact"][raw] == 3
    names = [_key(m["n"]) for m in payload["models"]]
    assert lineage["impact"][names.index("obs.orders")] == 2


@pytest.mark.usefixtures("collector")
def test_standalone_audit_is_logged_under_its_own_name(project: Path, probe: Probe) -> None:
    """A standalone audit isn't a model: SQLMesh's ``Snapshot.model`` raises for it."""
    from sqlmesh_observability.reader import load_report_data

    _install_on_load(probe)
    (project / "models" / "events.sql").unlink()
    (project / "audits").mkdir(exist_ok=True)
    (project / "audits" / "orders_positive.sql").write_text(
        "AUDIT (name assert_orders_positive, standalone true);\n"
        "SELECT * FROM obs.orders WHERE id < 0"
    )
    context = _chain(project)
    _plan(context)
    context.close()

    data = load_report_data(project)
    names = [_key(m.model_name) for m in data.models]
    assert "None" not in names and "" not in names
    (audit_model,) = [m for m in data.models if m.model_name.endswith("assert_orders_positive")]
    assert [(a.audit_name, a.status) for a in audit_model.audits] == [
        ("assert_orders_positive", "pass")
    ]
    assert audit_model.audits[0].snapshot_id
    node = next(n for n in data.lineage if n.model_name.endswith("assert_orders_positive"))
    assert node.kind == "AUDIT"
    assert [_key(p) for p in node.parents] == ["obs.orders"]
