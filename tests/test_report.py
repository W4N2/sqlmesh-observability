"""Tests: static HTML report and read-only reader."""

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

ONE_ROW = "SELECT CAST(@start_ds AS DATE) AS ds, 1 AS id"
FIVE_NULL_ROWS = "SELECT CAST(@start_ds AS DATE) AS ds, CAST(NULL AS INT) AS id FROM range(5)"


@pytest.fixture
def collector() -> t.Iterator["Collector"]:
    from sqlmesh_observability import collector as collector_module

    collector_module._reset_for_tests()
    yield collector_module.get_collector()
    collector_module._reset_for_tests()


def _install_on_load(probe: Probe, **kwargs: t.Any) -> None:
    from sqlmesh_observability import install

    probe.hook = lambda config: install(config, "local", **kwargs)


def _write_model(
    project: Path,
    filename: str,
    name: str,
    select: str,
    audits: str,
    owner: t.Optional[str] = None,
) -> None:
    owner_line = f"  owner '{owner}',\n" if owner else ""
    (project / "models" / filename).write_text(
        f"""
MODEL (
  name {name},
{owner_line}  kind INCREMENTAL_BY_TIME_RANGE (time_column ds, batch_size 1),
  cron '@daily',
  audits ({audits})
);

{select}
"""
    )


def _plan(context: "Context", environment: str = "prod", **kwargs: t.Any) -> t.Any:
    kwargs.setdefault("execution_time", PLAN_EXECUTION_TIME)
    return context.plan(environment, auto_apply=True, no_prompts=True, **kwargs)


# --- Task 2.1: reader.py unit tests ---------------------------------------------------------------


@pytest.mark.usefixtures("collector")
def test_reader_blocking_fail_and_pass_with_samples_and_state_metadata(
    project: Path, probe: Probe
) -> None:
    from sqlmesh.core.context import Context
    from sqlmesh.utils.errors import PlanError

    from sqlmesh_observability.reader import load_report_data

    _install_on_load(probe, sample_rows=2)
    _write_model(
        project,
        "events.sql",
        "obs.events",
        ONE_ROW,
        "not_null(columns := (id))",
        owner="analytics-team",
    )
    _write_model(
        project,
        "broken.sql",
        "obs.broken",
        FIVE_NULL_ROWS,
        "not_null(columns := (id))",
        owner="payments-team",
    )
    context = Context(paths=str(project))
    # First seed prod with a passing events model so prod state exists with owner & columns.
    (project / "models" / "broken.sql").unlink()
    context.load()
    _plan(context)

    # Now add a broken model whose blocking audit fails on prod plan (does not promote).
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

    data = load_report_data(project)

    assert data.environment == "prod"
    assert data.schema == SCHEMA
    assert data.failing_model_count == 1
    assert data.passing_audit_count == 2  # two intervals of obs.events

    failing_names = [m.model_name for m in data.failing_models]
    assert len(failing_names) == 1 and failing_names[0].endswith('"broken"')

    by_model = {m.model_name: m for m in data.models}
    events = next(m for name, m in by_model.items() if name.endswith('"events"'))
    assert events.owner == "analytics-team"
    assert {col for col, _ in events.columns} == {"ds", "id"}
    assert len(events.audits) == 2
    assert all(a.status == "pass" for a in events.audits)
    assert events.failing_blocking_audits == ()

    assert len(data.incidents) >= 1
    assert any(
        i.interval_start is not None and "2024-01-01" in i.interval_start for i in data.incidents
    )
    incident = data.incidents[0]
    assert incident.model_name.endswith('"broken"')
    assert incident.audit_name == "not_null"
    assert incident.status == "fail"
    assert incident.blocking is True
    assert incident.violation_count == 5
    assert incident.snapshot_id
    assert incident.query_sql
    assert incident.environment == "prod"
    assert len(incident.samples) == 2
    assert all(json.loads(row)["id"] is None for row in incident.samples)


@pytest.mark.usefixtures("collector")
def test_reader_no_samples_when_sampling_disabled(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context

    from sqlmesh_observability.reader import load_report_data

    _install_on_load(probe, sample_rows=0)
    _write_model(
        project,
        "events.sql",
        "obs.events",
        FIVE_NULL_ROWS,
        "not_null_non_blocking(columns := (id))",
    )
    context = Context(paths=str(project))
    _plan(context)
    context.close()

    data = load_report_data(project)
    assert len(data.incidents) == 2
    assert all(incident.samples == () for incident in data.incidents)


@pytest.mark.usefixtures("collector")
def test_reader_scopes_dev_vs_prod_and_includes_promoted_dev_build(
    project: Path, probe: Probe
) -> None:
    from sqlmesh.core.context import Context

    from sqlmesh_observability.reader import load_report_data

    _install_on_load(probe)
    _write_model(project, "events.sql", "obs.events", ONE_ROW, "not_null(columns := (id))")
    context = Context(paths=str(project))
    _plan(context)  # v1 passing in prod

    # Build v2 in dev with a non-blocking failure, then promote v2 to prod via virtual update.
    _write_model(
        project,
        "events.sql",
        "obs.events",
        "SELECT CAST(@start_ds AS DATE) AS ds, 2 AS id",
        "not_null(columns := (id))",
    )
    context.load()
    _plan(context, "dev", start="2024-01-01", end="2024-01-02")
    _plan(context)  # virtual update: v2 promoted to prod with no recompute

    # Now introduce an unpromoted dev-only failing audit on v3.
    _write_model(
        project,
        "events.sql",
        "obs.events",
        "SELECT CAST(@start_ds AS DATE) AS ds, 999 AS id",
        "accepted_values_non_blocking(column := id, is_in := (1, 2))",
    )
    context.load()
    _plan(context, "dev", start="2024-01-01", end="2024-01-02")
    context.close()

    prod = load_report_data(project, environment="prod")
    prod_audit_names = {a.audit_name for m in prod.models for a in m.audits}
    assert "not_null" in prod_audit_names
    assert "accepted_values_non_blocking" not in prod_audit_names
    # Dev-built v2 audit rows carry environment == "dev" and are visible in prod via state join.
    assert any(
        a.environment == "dev" and a.audit_name == "not_null" for m in prod.models for a in m.audits
    )

    dev = load_report_data(project, environment="dev")
    dev_audit_names = {a.audit_name for m in dev.models for a in m.audits}
    assert "accepted_values_non_blocking" in dev_audit_names
    assert len(dev.incidents) >= 1


@pytest.mark.usefixtures("collector")
def test_reader_missing_audit_table_raises(project: Path, probe: Probe) -> None:
    from sqlmesh_observability.reader import MissingAuditTableError, load_report_data

    _install_on_load(probe)
    with pytest.raises(MissingAuditTableError) as exc_info:
        load_report_data(project, schema="nonexistent_obs")
    assert exc_info.value.schema == "nonexistent_obs"
    assert "nonexistent_obs" in str(exc_info.value)


# --- Task 3.1: HTML report rendering tests (stdlib html.parser, no browser) -----------------------


class _SectionTextParser:
    """Minimal stdlib HTMLParser wrapper capturing visible text per element id."""

    def __init__(self, document: str) -> None:
        from html.parser import HTMLParser

        sections: t.Dict[str, t.List[str]] = {}
        all_text: t.List[str] = []
        tags_seen: t.List[str] = []
        id_stack: t.List[t.Optional[str]] = []
        skip_depth = 0

        class _Parser(HTMLParser):
            def handle_starttag(
                self, tag: str, attrs: t.List[t.Tuple[str, t.Optional[str]]]
            ) -> None:
                nonlocal skip_depth
                tags_seen.append(tag)
                if tag in ("style", "script"):
                    skip_depth += 1
                attr_map = dict(attrs)
                elem_id = attr_map.get("id")
                id_stack.append(elem_id)
                if elem_id is not None:
                    sections.setdefault(elem_id, [])

            def handle_endtag(self, tag: str) -> None:
                nonlocal skip_depth
                if tag in ("style", "script") and skip_depth > 0:
                    skip_depth -= 1
                if id_stack:
                    id_stack.pop()

            def handle_data(self, data: str) -> None:
                if skip_depth > 0:
                    return
                text = data.strip()
                if not text:
                    return
                all_text.append(text)
                for elem_id in id_stack:
                    if elem_id is not None:
                        sections.setdefault(elem_id, []).append(text)

        parser = _Parser()
        parser.feed(document)
        parser.close()

        self.all_text = " ".join(all_text)
        self.sections = {key: " ".join(parts) for key, parts in sections.items()}
        self.tags_seen = tags_seen


@pytest.mark.usefixtures("collector")
def test_report_html_contains_failure(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context
    from sqlmesh.utils.errors import PlanError

    from sqlmesh_observability.reader import load_report_data
    from sqlmesh_observability.report import render_report

    _install_on_load(probe, sample_rows=2)
    _write_model(
        project,
        "events.sql",
        "obs.events",
        ONE_ROW,
        "not_null(columns := (id))",
        owner="analytics-team",
    )
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

    data = load_report_data(project)
    html_doc = render_report(data)
    parsed = _SectionTextParser(html_doc)

    # Overview failure list has the failing model & audit, NOT the passing-only model.
    failures_text = parsed.sections["overview-failures"]
    assert "broken" in failures_text
    assert "not_null" in failures_text
    assert "events" not in failures_text

    # Model history includes passing model, its owner, columns, and passing status.
    models_text = parsed.sections["models"]
    assert "events" in models_text
    assert "analytics-team" in models_text
    assert "pass" in models_text
    assert "ds" in models_text and "id" in models_text

    # Incident section carries full failure facts and sample rows.
    incidents_text = parsed.sections["incidents"]
    incident = data.incidents[0]
    assert "broken" in incidents_text
    assert "not_null" in incidents_text
    assert "5" in incidents_text
    assert "2024-01-01" in incidents_text
    assert incident.snapshot_id is not None and incident.snapshot_id in incidents_text
    assert "SELECT" in incidents_text.upper()
    assert "prod" in incidents_text
    assert '"id": null' in incidents_text


@pytest.mark.usefixtures("collector")
def test_report_html_omits_samples_when_none_stored(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context

    from sqlmesh_observability.reader import load_report_data
    from sqlmesh_observability.report import render_report

    _install_on_load(probe, sample_rows=0)
    _write_model(
        project,
        "events.sql",
        "obs.events",
        FIVE_NULL_ROWS,
        "not_null_non_blocking(columns := (id))",
    )
    context = Context(paths=str(project))
    _plan(context)
    context.close()

    html_doc = render_report(load_report_data(project))
    parsed = _SectionTextParser(html_doc)
    incidents_text = parsed.sections["incidents"]
    assert "not_null_non_blocking" in incidents_text
    assert "Sample rows" not in incidents_text
    assert '"id": null' not in html_doc


def test_report_html_escapes_untrusted_text_and_contains_no_script_tags() -> None:
    from sqlmesh_observability.reader import AuditRecord, ModelReport, ReportData
    from sqlmesh_observability.report import render_report

    record = AuditRecord(
        invocation_id="inv-1",
        model_name='obs."orders"',
        snapshot_id="snap-1",
        version="v1",
        physical_table="obs__orders__v1",
        environment="prod",
        audit_name="check_<script>alert(1)</script>",
        args_json="{}",
        interval_start="2024-01-01 00:00:00",
        interval_end="2024-01-02 00:00:00",
        status="fail",
        violation_count=3,
        blocking=True,
        query_sql="SELECT * FROM t WHERE x < 10 AND y = '<script>boom()</script>'",
        error_message=None,
        started_at="2024-01-03 00:00:00",
        samples=('{"note": "<img src=x onerror=alert(1)>"}',),
    )
    model = ModelReport(
        model_name=record.model_name,
        owner="team<a&b>",
        columns=(("id", "INT"), ("note", "TEXT")),
        audits=(record,),
        failing_blocking_audits=(record,),
    )
    data = ReportData(
        environment="prod",
        schema="observability",
        gateway="warehouse",
        generated_at="2024-01-03 00:00:00 UTC",
        failing_model_count=1,
        passing_audit_count=0,
        failing_models=(model,),
        models=(model,),
        incidents=(record,),
    )

    html_doc = render_report(data)
    parsed = _SectionTextParser(html_doc)
    # The only script is the report's own filter script; injected text stays text.
    assert parsed.tags_seen.count("script") == 1
    assert html_doc.count("<script") == 1 and '<script id="report-filters">' in html_doc
    assert "img" not in parsed.tags_seen
    assert "<script>alert(1)</script>" in parsed.all_text
    assert "team<a&b>" in parsed.all_text


# --- Task 4.1: CLI tests --------------------------------------------------------------------------


@pytest.mark.usefixtures("collector")
def test_report_defaults_to_prod(project: Path, probe: Probe, tmp_path: Path) -> None:
    from sqlmesh.core.context import Context

    from sqlmesh_observability.cli import main

    _install_on_load(probe)
    _write_model(project, "events.sql", "obs.events", ONE_ROW, "not_null(columns := (id))")
    context = Context(paths=str(project))
    _plan(context)

    _write_model(
        project,
        "events.sql",
        "obs.events",
        "SELECT CAST(@start_ds AS DATE) AS ds, 999 AS id",
        "accepted_values_non_blocking(column := id, is_in := (1, 2))",
    )
    context.load()
    _plan(context, "dev", start="2024-01-01", end="2024-01-02")
    context.close()

    prod_out = tmp_path / "prod.html"
    rc = main(["report", "--path", str(project), "--output", str(prod_out)])
    assert rc == 0
    prod_text = _SectionTextParser(prod_out.read_text(encoding="utf-8")).all_text
    assert "not_null" in prod_text
    assert "accepted_values_non_blocking" not in prod_text

    dev_out = tmp_path / "dev.html"
    rc = main(["report", "--path", str(project), "--environment", "dev", "--output", str(dev_out)])
    assert rc == 0
    dev_text = _SectionTextParser(dev_out.read_text(encoding="utf-8")).all_text
    assert "accepted_values_non_blocking" in dev_text


@pytest.mark.usefixtures("collector")
def test_report_without_project_files(project: Path, probe: Probe, tmp_path: Path) -> None:
    """Run ``sqlmesh-observe report`` in a separate process whose cwd has no model files."""
    import os
    import subprocess
    import sys

    from sqlmesh.core.context import Context
    from sqlmesh.utils.errors import PlanError

    _install_on_load(probe, sample_rows=1)
    _write_model(project, "events.sql", "obs.events", FIVE_NULL_ROWS, "not_null(columns := (id))")
    context = Context(paths=str(project))
    with pytest.raises(PlanError):
        _plan(context)
    context.close()

    # Separate directory containing ONLY a minimal config.py pointing at the same DuckDB file.
    viewer = tmp_path / "viewer"
    viewer.mkdir()
    database = tmp_path / "warehouse.duckdb"
    secret_marker = "SUPER_SECRET_PASSWORD_DO_NOT_LEAK"
    (viewer / "config.py").write_text(
        f"""
from sqlmesh.core.config import (
    Config,
    DuckDBConnectionConfig,
    GatewayConfig,
    ModelDefaultsConfig,
)

# Synthetic credential marker in variables to confirm config secrets are never embedded.
config = Config(
    gateways={{
        "local": GatewayConfig(connection=DuckDBConnectionConfig(database={str(database)!r}))
    }},
    default_gateway="local",
    model_defaults=ModelDefaultsConfig(dialect="duckdb"),
    variables={{"db_password": {secret_marker!r}}},
)
"""
    )

    observe_bin = str(Path(sys.executable).parent / "sqlmesh-observe")
    output = viewer / "out.html"
    done = subprocess.run(
        [
            observe_bin,
            "report",
            "--gateway",
            "local",
            "--schema",
            SCHEMA,
            "--output",
            str(output),
        ],
        cwd=str(viewer),
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "SQLMESH__DISABLE_ANONYMIZED_ANALYTICS": "1"},
    )
    assert done.returncode == 0, done.stdout + done.stderr
    html_doc = output.read_text(encoding="utf-8")
    parsed = _SectionTextParser(html_doc)
    assert "not_null" in parsed.all_text
    assert "events" in parsed.all_text
    assert "5" in parsed.all_text
    assert secret_marker not in html_doc


@pytest.mark.usefixtures("collector")
def test_report_missing_schema_exits_nonzero(
    project: Path, probe: Probe, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from sqlmesh_observability.cli import main

    _install_on_load(probe)
    out = tmp_path / "missing.html"
    rc = main(
        [
            "report",
            "--path",
            str(project),
            "--schema",
            "missing_obs_schema",
            "--output",
            str(out),
        ]
    )
    assert rc != 0
    assert not out.exists()
    captured = capsys.readouterr()
    assert "missing_obs_schema" in captured.err


@pytest.mark.usefixtures("collector")
def test_report_inherits_gateway_and_schema_from_install(tmp_path: Path) -> None:
    from sqlmesh.core.context import Context

    from sqlmesh_observability.cli import main

    root = tmp_path / "custom_project"
    (root / "models").mkdir(parents=True)
    _write_model(root, "events.sql", "obs.events", ONE_ROW, "not_null(columns := (id))")
    database = tmp_path / "custom.duckdb"
    (root / "config.py").write_text(
        f"""
from sqlmesh.core.config import (
    Config,
    DuckDBConnectionConfig,
    GatewayConfig,
    ModelDefaultsConfig,
)
from sqlmesh_observability import install

config = install(
    Config(
        gateways={{
            "obs_gw": GatewayConfig(connection=DuckDBConnectionConfig(database={str(database)!r}))
        }},
        default_gateway="obs_gw",
        model_defaults=ModelDefaultsConfig(dialect="duckdb", start="2024-01-01"),
    ),
    "obs_gw",
    schema="custom_obs",
)
"""
    )
    context = Context(paths=str(root))
    _plan(context)
    context.close()

    out = tmp_path / "inherited.html"
    rc = main(["report", "--path", str(root), "-o", str(out)])
    assert rc == 0
    parsed = _SectionTextParser(out.read_text(encoding="utf-8"))
    assert "custom_obs" in parsed.all_text
    assert "not_null" in parsed.all_text


# --- Review fixes (2026-11-25) --------------------------------------------------------------------


def _ids(document: str) -> t.List[str]:
    import re

    return re.findall(r'\sid="([^"]+)"', document)


@pytest.mark.usefixtures("collector")
def test_same_audit_with_different_args_stays_separate(project: Path, probe: Probe) -> None:
    """A model may list one audit twice with different args; neither may hide the other."""
    from sqlmesh.core.context import Context
    from sqlmesh.utils.errors import PlanError

    from sqlmesh_observability.reader import load_report_data
    from sqlmesh_observability.report import render_report

    _install_on_load(probe)
    _write_model(
        project,
        "events.sql",
        "obs.events",
        "SELECT CAST(@start_ds AS DATE) AS ds, CAST(NULL AS INT) AS id, 1 AS other",
        # The passing use is listed first, so it would win a tie on (audit_name, interval).
        "not_null(columns := (other)), not_null(columns := (id))",
    )
    context = Context(paths=str(project))
    with pytest.raises(PlanError):
        _plan(context)
    context.close()

    data = load_report_data(project)
    (model,) = data.models
    failing_args = {a.args_json for a in model.failing_blocking_audits}
    assert failing_args and all(args is not None and "(id)" in args for args in failing_args)
    assert any(a.status == "pass" and "(other)" in (a.args_json or "") for a in model.audits)

    html_doc = render_report(data)
    ids = _ids(html_doc)
    assert len(ids) == len(set(ids)), "duplicate element ids"
    parsed = _SectionTextParser(html_doc)
    assert "columns := (id)" in parsed.sections["overview-failures"]
    models_text = parsed.sections["models"]
    assert "columns := (id)" in models_text and "columns := (other)" in models_text


@pytest.mark.usefixtures("collector")
def test_overview_lists_non_blocking_failures_as_warnings(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context

    from sqlmesh_observability.reader import load_report_data
    from sqlmesh_observability.report import render_report

    _install_on_load(probe)
    _write_model(
        project,
        "events.sql",
        "obs.events",
        FIVE_NULL_ROWS,
        "not_null_non_blocking(columns := (id))",
    )
    context = Context(paths=str(project))
    _plan(context)
    context.close()

    data = load_report_data(project)
    assert data.failing_model_count == 0
    assert data.warning_model_count == 1
    (model,) = data.warning_models
    assert model.model_name.endswith('"events"')
    assert {a.audit_name for a in model.warning_audits} == {"not_null_non_blocking"}

    parsed = _SectionTextParser(render_report(data))
    assert "events" in parsed.sections["overview-warnings"]
    assert "events" not in parsed.sections["overview-failures"]


@pytest.mark.usefixtures("collector")
def test_report_says_when_state_is_unavailable(
    project: Path, probe: Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlmesh.core.context import Context

    from sqlmesh_observability import reader
    from sqlmesh_observability.report import render_report

    _install_on_load(probe)
    _write_model(project, "events.sql", "obs.events", ONE_ROW, "not_null(columns := (id))")
    context = Context(paths=str(project))
    _plan(context)
    context.close()

    assert reader.load_report_data(project).state_available is True

    monkeypatch.setattr(reader, "_read_state_from_gateway", lambda *args: None)
    data = reader.load_report_data(project)
    assert data.state_available is False
    parsed = _SectionTextParser(render_report(data))
    assert "state" in parsed.sections["state-warning"].lower()


def test_state_is_read_from_the_default_gateway_first(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from sqlmesh_observability import reader

    calls: t.List[str] = []

    def fake(config: t.Any, gateway: str, environment: str) -> None:
        calls.append(gateway)
        return None

    monkeypatch.setattr(reader, "_read_state_from_gateway", fake)
    reader._read_environment_state(SimpleNamespace(default_gateway="main"), "obs_log", "prod")
    assert calls == ["main", "obs_log"]


@pytest.mark.usefixtures("collector")
def test_audit_rows_are_filtered_in_sql(
    project: Path, probe: Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader must not pull the whole audit_results table and filter it in Python."""
    from sqlglot import exp
    from sqlmesh.core.context import Context
    from sqlmesh.core.engine_adapter import EngineAdapter

    from sqlmesh_observability.reader import load_report_data

    _install_on_load(probe)
    _write_model(project, "events.sql", "obs.events", ONE_ROW, "not_null(columns := (id))")
    context = Context(paths=str(project))
    _plan(context)
    context.close()

    queries: t.List[exp.Expression] = []
    original = EngineAdapter.fetchall

    def spy(self: t.Any, query: t.Any, *args: t.Any, **kwargs: t.Any) -> t.Any:
        if isinstance(query, exp.Expression):
            queries.append(query)
        return original(self, query, *args, **kwargs)

    monkeypatch.setattr(EngineAdapter, "fetchall", spy)
    data = load_report_data(project)
    assert data.models

    audit_queries = [
        q for q in queries if any(table.name == "audit_results" for table in q.find_all(exp.Table))
    ]
    assert audit_queries
    for query in audit_queries:
        where = query.args.get("where")
        assert where is not None, query.sql()
        assert any(col.name == "version" for col in where.find_all(exp.Column)), query.sql()


@pytest.mark.usefixtures("collector")
def test_state_metadata_survives_a_fresh_process(project: Path, probe: Probe) -> None:
    """Reading snapshots forks SQLMesh's process pool. On macOS the children crash unless pandas
    is imported first (as SQLMesh's own loader does), which silently dropped owners and columns
    in a plain ``sqlmesh-observe report`` process. pytest has pandas loaded already, so this runs
    the read in a fresh interpreter."""
    import subprocess
    import sys

    from sqlmesh.core.context import Context

    _install_on_load(probe)
    _write_model(
        project, "events.sql", "obs.events", ONE_ROW, "not_null(columns := (id))", owner="ops"
    )
    context = Context(paths=str(project))
    _plan(context)
    context.close()
    (project / "config.py").write_text(
        "from sqlmesh.core.config import Config, DuckDBConnectionConfig, GatewayConfig, "
        "ModelDefaultsConfig\n"
        "config = Config(gateways={'local': GatewayConfig(connection=DuckDBConnectionConfig("
        f"database={str(project.parent / 'warehouse.duckdb')!r}))}}, default_gateway='local', "
        "model_defaults=ModelDefaultsConfig(dialect='duckdb'))\n"
    )
    script = (
        "import sys\n"
        "from sqlmesh_observability.reader import load_report_data\n"
        "data = load_report_data(sys.argv[1])\n"
        "print([(m.owner, len(m.columns)) for m in data.models])\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(project)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.stdout.strip() == "[('ops', 2)]", result.stderr[-2000:]
