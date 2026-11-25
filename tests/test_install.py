"""Tests: ``sqlmesh_observability.install()``."""

from __future__ import annotations

import dataclasses
import datetime
import typing as t
from pathlib import Path

import pytest
from conftest import PLAN_EXECUTION_TIME, RUN_EXECUTION_TIME, Probe

if t.TYPE_CHECKING:
    from sqlmesh.core.context import Context

    from sqlmesh_observability.collector import Collector


@pytest.fixture
def collector() -> t.Iterator["Collector"]:
    from sqlmesh_observability import collector as collector_module

    collector_module._reset_for_tests()
    yield collector_module.get_collector()
    collector_module._reset_for_tests()


@pytest.fixture
def installed(probe: Probe, collector: "Collector") -> Probe:
    """``config.py`` ends with ``config = install(Config(...), connection)``."""
    from sqlmesh_observability import install

    probe.hook = lambda config: install(config, "local")
    return probe


def _plan(context: "Context", environment: str = "prod", **kwargs: t.Any) -> t.Any:
    kwargs.setdefault("execution_time", PLAN_EXECUTION_TIME)
    return context.plan(environment, auto_apply=True, no_prompts=True, **kwargs)


def _edit_model(project: Path, old: str, new: str) -> None:
    path = project / "models" / "events.sql"
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


# --- Console wrap --------------------------------------------------------------------------------


@pytest.mark.usefixtures("installed")
def test_install_cli_console_records_rows(project: Path, collector: "Collector") -> None:
    from click.testing import CliRunner
    from sqlmesh.cli.main import cli

    runner = CliRunner()
    for args in (
        ["plan", "--auto-apply", "--no-prompts", "--execution-time", PLAN_EXECUTION_TIME],
        ["run", "--end", "2024-01-04"],  # no --execution-time; --end is inclusive
    ):
        result = runner.invoke(cli, ["--paths", str(project), *args], catch_exceptions=False)
        assert result.exit_code == 0, result.output

    plan, run = collector.finished
    assert (plan.command, run.command) == ("plan", "run")
    for invocation in (plan, run):
        assert invocation.status == "success"
        assert len(invocation.models) == 2
        assert len(invocation.audits) == 2
        assert {audit.audit_name for audit in invocation.audits} == {"not_null"}


@pytest.mark.usefixtures("installed")
def test_config_loads_before_console_capture(project: Path, probe: Probe) -> None:
    """Order pin with the real install(): the console wrapped at load is the one Context uses."""
    from sqlmesh.core.console import configure_console
    from sqlmesh.core.context import Context

    from sqlmesh_observability.hook import is_wrapped

    configure_console()
    context = Context(paths=str(project))

    assert probe.loads[-1] is context.console
    assert is_wrapped(context.console)


def test_install_returns_same_config(collector: "Collector") -> None:
    from sqlmesh.core.config import Config

    from sqlmesh_observability import install

    config = Config()
    assert install(config, "local") is config


@pytest.mark.usefixtures("installed")
def test_install_chains_existing_observer(project: Path, probe: Probe) -> None:
    from sqlmesh.core.console import get_console
    from sqlmesh.core.context import Context

    from sqlmesh_observability import install

    def sequence_of_one_plan(project_dir: Path, with_install: bool) -> t.List[t.Tuple[str, str]]:
        seen: t.List[t.Tuple[str, str]] = []

        def existing(phase: str, facts: t.Mapping[str, t.Any], error: t.Any) -> None:
            seen.append((phase, f"{facts['kind']}/{facts['action']}"))

        def hook(config: t.Any) -> t.Any:
            get_console().get_execution_observer = lambda: existing  # type: ignore[method-assign]
            return install(config, "local") if with_install else config

        probe.hook = hook
        context = Context(paths=str(project_dir))
        _plan(context)
        context.close()  # release the DuckDB file so the next run starts from empty state
        return seen

    baseline = sequence_of_one_plan(project, with_install=False)
    for path in (project.parent / "warehouse.duckdb", project.parent / "warehouse.duckdb.wal"):
        path.unlink(missing_ok=True)
    chained = sequence_of_one_plan(project, with_install=True)

    assert baseline and chained == baseline


@pytest.mark.usefixtures("installed")
def test_collector_error_is_isolated(
    project: Path, probe: Probe, collector: "Collector", monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlmesh.core.console import get_console
    from sqlmesh.core.context import Context

    from sqlmesh_observability import install

    seen: t.List[str] = []

    def existing(phase: str, facts: t.Mapping[str, t.Any], error: t.Any) -> None:
        seen.append(phase)

    def hook(config: t.Any) -> t.Any:
        get_console().get_execution_observer = lambda: existing  # type: ignore[method-assign]
        return install(config, "local")

    def boom(*args: t.Any, **kwargs: t.Any) -> None:
        raise RuntimeError("collector failure")

    probe.hook = hook
    monkeypatch.setattr(type(collector), "handle", boom)
    monkeypatch.setattr(type(collector), "arm", boom)
    monkeypatch.setattr(type(collector), "end", boom)
    context = Context(paths=str(project))

    _plan(context)  # does not raise
    context.run(execution_time=RUN_EXECUTION_TIME)

    assert seen.count("start") == seen.count("finish") > 0
    assert context.state_sync.get_environment("prod") is not None


@pytest.mark.usefixtures("installed")
def test_install_is_idempotent(project: Path, collector: "Collector") -> None:
    from sqlmesh.core.config import Config
    from sqlmesh.core.console import configure_console
    from sqlmesh.core.context import Context

    from sqlmesh_observability import install
    from sqlmesh_observability.hook import ObservabilityTarget

    configure_console()
    Context(paths=str(project))  # first load of config.py wraps the console
    context = Context(paths=str(project))  # second load, same console
    _plan(context)

    (invocation,) = collector.finished
    assert len(invocation.models) == 2
    assert len(invocation.audits) == 2

    config = Config()
    install(install(config, "local"), "local")
    assert sum(isinstance(t, ObservabilityTarget) for t in config.notification_targets) == 1


# --- Notification target and invocation boundaries ------------------------------------------------


@pytest.mark.usefixtures("installed")
def test_plan_is_one_invocation(project: Path, collector: "Collector") -> None:
    from sqlmesh.core.context import Context

    context = Context(paths=str(project))
    plan = _plan(context)
    context.run(execution_time=RUN_EXECUTION_TIME)

    first, second = collector.finished
    assert first.invocation_id != second.invocation_id
    assert (first.command, first.environment, first.plan_id) == ("plan", "prod", plan.plan_id)
    assert (second.command, second.environment, second.plan_id) == ("run", "prod", None)
    for invocation in (first, second):
        ids = {r.invocation_id for r in [*invocation.models, *invocation.audits]}
        assert ids == {invocation.invocation_id}


@pytest.mark.usefixtures("installed")
def test_failed_apply_is_handed_off_with_failure(project: Path, collector: "Collector") -> None:
    from sqlmesh.core.context import Context
    from sqlmesh.utils.errors import PlanError

    _edit_model(project, "1 AS id", "NULL::INT AS id")
    context = Context(paths=str(project))

    with pytest.raises(PlanError):
        _plan(context)

    (invocation,) = collector.finished
    assert invocation.status == "failure"
    assert invocation.environment == "prod"


@pytest.mark.usefixtures("installed")
def test_nothing_to_do_run_does_not_mislabel_next_plan(
    project: Path, collector: "Collector"
) -> None:
    from sqlmesh.core.context import Context

    context = Context(paths=str(project))
    _plan(context)
    context.run(execution_time=PLAN_EXECUTION_TIME)  # nothing to do, no end hook

    _edit_model(project, "1 AS id", "2 AS id")
    context.load()
    dev_plan = _plan(context, "dev", start="2024-01-01", end="2024-01-02")

    assert [i.environment for i in collector.finished] == ["prod", "dev"]
    assert collector.finished[-1].plan_id == dev_plan.plan_id
    assert collector.finished[-1].status == "success"


@pytest.mark.usefixtures("installed")
def test_two_applies_in_one_process_hand_off_twice(project: Path, collector: "Collector") -> None:
    """Simulates the long-lived ``sqlmesh ui`` process: no exit between applies."""
    from sqlmesh.core.context import Context

    context = Context(paths=str(project))
    _plan(context)
    _edit_model(project, "1 AS id", "2 AS id")
    context.load()
    _plan(context)

    assert [i.status for i in collector.finished] == ["success", "success"]
    assert len({i.invocation_id for i in collector.finished}) == 2


@pytest.mark.usefixtures("installed")
def test_install_keeps_api_console(project: Path, collector: "Collector") -> None:
    from concurrent.futures import ThreadPoolExecutor

    from sqlmesh.core.console import get_console
    from web.server import models
    from web.server.api.endpoints.commands import _run_plan_apply
    from web.server.console import api_console
    from web.server.settings import _get_context

    context = _get_context.__wrapped__(str(project), "", None)
    context.load()
    assert get_console() is api_console

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(
            _run_plan_apply,
            context,
            "dev",
            models.PlanOptions(),
            models.PlanDates(start="2024-01-01", end="2024-01-02"),
        ).result()

    assert get_console() is api_console
    (invocation,) = collector.finished
    assert invocation.environment == "dev"
    assert invocation.models and {m.environment for m in invocation.models} == {"dev"}


# --- Target capture ------------------------------------------------------------------------------


@pytest.mark.usefixtures("installed")
def test_environment_and_target_facts(project: Path, collector: "Collector") -> None:
    from sqlmesh.core.context import Context

    context = Context(paths=str(project))
    _plan(context)
    prod_table = context.table_name("obs.events", prod=True)

    # Breaking change in dev: builds the deployable table a later promotion reuses.
    _edit_model(project, "1 AS id", "2 AS id")
    context.load()
    _plan(context, "dev", start="2024-01-01", end="2024-01-02")
    dev_deployable = context.table_name("obs.events", environment="dev", prod=True)

    # Forward-only change in dev: writes the dev preview table, not the prod table.
    _edit_model(project, "2 AS id", "3 AS id")
    context.load()
    _plan(
        context, "fo", start="2024-01-01", end="2024-01-02", forward_only=True, enable_preview=True
    )
    preview_table = context.table_name("obs.events", environment="fo", prod=False)

    prod, dev, fo = collector.finished
    assert {(m.environment, m.physical_table) for m in prod.models} == {("prod", prod_table)}
    assert {(m.environment, m.physical_table) for m in dev.models} == {("dev", dev_deployable)}
    assert dev_deployable != prod_table
    assert {(m.environment, m.physical_table) for m in fo.models} == {("fo", preview_table)}
    assert preview_table not in (prod_table, dev_deployable)

    first = prod.models[0]
    assert first.model_name == '"warehouse"."obs"."events"'
    assert first.version and first.snapshot_id and first.status == "success"
    assert sorted((m.interval_start, m.interval_end) for m in prod.models) == [
        (_ms("2024-01-01"), _ms("2024-01-02")),
        (_ms("2024-01-02"), _ms("2024-01-03")),
    ]
    # Each audit is linked to the model interval it audited.
    assert sorted((a.interval_start, a.model_name) for a in prod.audits) == sorted(
        (m.interval_start, m.model_name) for m in prod.models
    )


def _ms(day: str) -> int:
    from sqlmesh.utils.date import to_timestamp

    return to_timestamp(day)


@pytest.mark.usefixtures("installed")
def test_records_hold_plain_values_only(project: Path, collector: "Collector") -> None:
    from sqlmesh.core.context import Context

    _plan(Context(paths=str(project)))

    plain = (str, int, float, bool, type(None), datetime.datetime)
    (invocation,) = collector.finished
    for record in [invocation, *invocation.models, *invocation.audits]:
        for field in dataclasses.fields(record):
            value = getattr(record, field.name)
            if field.name in ("models", "audits", "schemas"):
                continue
            if isinstance(value, tuple):  # column metadata: tuples of plain values
                assert all(isinstance(v, plain) for item in value for v in item), field.name
                continue
            assert isinstance(value, plain), (type(record).__name__, field.name, type(value))


# --- Coexistence with console-replacing packages (sqlmesh-openlineage) --------------------------


class DelegatingConsole:
    """Same shape as ``sqlmesh_openlineage.console.OpenLineageConsole``: delegation, no subclass."""

    def __init__(self, wrapped: t.Any) -> None:
        self._wrapped = wrapped
        self.evaluations_started = 0

    def __getattr__(self, name: str) -> t.Any:
        return getattr(self._wrapped, name)

    def start_snapshot_evaluation_progress(self, snapshot: t.Any, audit_only: bool = False) -> None:
        self.evaluations_started += 1
        self._wrapped.start_snapshot_evaluation_progress(snapshot, audit_only)


def _openlineage_style_install() -> DelegatingConsole:
    """Mirrors ``sqlmesh_openlineage.install()``: set_console(Wrapper(create_console()))."""
    from sqlmesh.core.console import create_console, set_console

    console = DelegatingConsole(create_console())
    set_console(console)  # type: ignore[arg-type]
    return console


@pytest.mark.parametrize("other_first", [True, False], ids=["other-first", "other-after"])
def test_coexists_with_console_replacing_package(
    project: Path, probe: Probe, collector: "Collector", other_first: bool
) -> None:
    from sqlmesh.core.context import Context

    from sqlmesh_observability import install

    holder: t.Dict[str, DelegatingConsole] = {}

    def hook(config: t.Any) -> t.Any:
        if other_first:
            holder["console"] = _openlineage_style_install()
            return install(config, "local")
        config = install(config, "local")
        holder["console"] = _openlineage_style_install()
        return config

    probe.hook = hook
    context = Context(paths=str(project))
    assert context.console is holder["console"]

    _plan(context)
    context.run(execution_time=RUN_EXECUTION_TIME)

    assert [i.command for i in collector.finished] == ["plan", "run"]
    assert all(len(i.models) == 2 for i in collector.finished)
    assert holder["console"].evaluations_started == 4  # the other package still sees its events
