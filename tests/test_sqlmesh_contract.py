"""Contract tests: SQLMesh behavior that ``install()`` relies on .

These use only SQLMesh's public surface plus test doubles. They do not import the package, so a
failure here means SQLMesh changed underneath us, not that the package broke. Each test cites the
SQLMesh code it pins (paths relative to the SQLMesh repository).
"""

from __future__ import annotations

import threading
import typing as t
from pathlib import Path

import pytest
from conftest import PLAN_EXECUTION_TIME, RUN_EXECUTION_TIME, Probe

if t.TYPE_CHECKING:
    from sqlmesh.core.console import Console


class Recorder:
    """Instance-level observer wrap, the same shape ``install()`` uses."""

    def __init__(self) -> None:
        self.resolutions = 0
        self.events: t.List[t.Tuple[int, str, t.Dict[str, t.Any], t.Optional[BaseException]]] = []
        self._lock = threading.Lock()

    def attach(self, console: "Console") -> None:
        def get_execution_observer() -> t.Callable[..., None]:
            with self._lock:
                self.resolutions += 1
                resolution = self.resolutions

            def observe(phase: str, facts: t.Mapping[str, t.Any], error: t.Any) -> None:
                copied = {k: v for k, v in facts.items() if k in ("kind", "action", "target")}
                with self._lock:
                    self.events.append((resolution, phase, copied, error))

            return observe

        console.get_execution_observer = get_execution_observer  # type: ignore[method-assign]

    def finished(self, kind: str, action: t.Optional[str] = None) -> t.List[t.Dict[str, t.Any]]:
        return [
            facts
            for _, phase, facts, _ in self.events
            if phase == "finish"
            and facts["kind"] == kind
            and (action is None or facts["action"] == action)
        ]


def _wrap_on_config_load(probe: Probe, recorder: Recorder) -> None:
    from sqlmesh.core.console import get_console

    def hook(config: t.Any) -> t.Any:
        recorder.attach(get_console())
        return config

    probe.hook = hook


# --- Assumption 1: config.py is loaded before Context captures the console --------------------


def test_config_loads_before_console_capture(project: Path, probe: Probe) -> None:
    """Order pin. sqlmesh/core/context.py: load_configs (~400) before self.console (~471)."""
    from sqlmesh.core.console import configure_console, get_console
    from sqlmesh.core.context import Context

    configure_console()
    context = Context(paths=str(project))

    assert probe.loads, "config.py was not executed"
    assert probe.loads[-1] is context.console
    assert context.console is get_console()


def test_cli_entrypoint_wraps_the_console_it_uses(project: Path, probe: Probe) -> None:
    """sqlmesh/cli/main.py: configure_console (~129) then load_configs, then Context (~160)."""
    from click.testing import CliRunner
    from sqlmesh.cli.main import cli

    recorder = Recorder()
    _wrap_on_config_load(probe, recorder)

    result = CliRunner().invoke(
        cli,
        ["--paths", str(project), "plan", "--auto-apply", "--no-prompts"]
        + ["--execution-time", PLAN_EXECUTION_TIME],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert recorder.finished("model", "evaluate")
    assert recorder.finished("audit", "audit")


def test_ui_settings_load_config_after_api_console(project: Path, probe: Probe) -> None:
    """web/server/settings.py _get_context: set_console(api_console) then Context(...)."""
    from sqlmesh.core.console import get_console
    from web.server.console import api_console
    from web.server.settings import _get_context

    context = _get_context.__wrapped__(str(project), "", None)  # bypass lru_cache

    assert probe.loads[-1] is api_console
    assert context.console is api_console
    assert get_console() is api_console


# --- Assumption 2: the observer getter is resolved once per outer plan or run -----------------


def test_plan_resolves_observer_once(project: Path, probe: Probe) -> None:
    """sqlmesh/core/execution_observation.py console_observer_scope skips nested scopes.

    The plan evaluator (plan/evaluator.py ~106) opens the outer scope. The scheduler it runs
    (scheduler.py ~462) sees ``_scoped`` and does not resolve the getter again.
    """
    from sqlmesh.core.context import Context

    recorder = Recorder()
    _wrap_on_config_load(probe, recorder)
    context = Context(paths=str(project))

    context.plan(auto_apply=True, no_prompts=True, execution_time=PLAN_EXECUTION_TIME)

    assert recorder.resolutions == 1
    assert {resolution for resolution, *_ in recorder.events} == {1}
    assert recorder.finished("stage"), "plan stages were not observed"
    assert len(recorder.finished("model", "evaluate")) == 2  # 2024-01-01 and 2024-01-02


def test_run_resolves_observer_once(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context

    recorder = Recorder()
    _wrap_on_config_load(probe, recorder)
    context = Context(paths=str(project))
    context.plan(auto_apply=True, no_prompts=True, execution_time=PLAN_EXECUTION_TIME)

    context.run(execution_time=RUN_EXECUTION_TIME)

    assert recorder.resolutions == 2  # one for the plan, one for the run
    run_events = [e for e in recorder.events if e[0] == 2]
    assert not [e for e in run_events if e[2]["kind"] == "stage"]
    assert len([e for e in run_events if e[1] == "finish" and e[2]["kind"] == "model"]) == 2


# --- Assumption 3: a notification target sees start/end with the environment ------------------


def _notification_target_class() -> t.Type[t.Any]:
    from sqlmesh.core.notification_target import GenericNotificationTarget

    class RecordingTarget(GenericNotificationTarget):
        """Records every hook call. Frozen pydantic model, so state lives in a class-level list."""

        def notify_apply_start(self, environment: str, plan_id: str, *a: t.Any, **k: t.Any) -> None:
            CALLS.append(("apply_start", environment, plan_id))

        def notify_apply_end(self, environment: str, plan_id: str, *a: t.Any, **k: t.Any) -> None:
            CALLS.append(("apply_end", environment, plan_id))

        def notify_apply_failure(
            self, environment: str, plan_id: str, exc: str, *a: t.Any, **k: t.Any
        ) -> None:
            CALLS.append(("apply_failure", environment, plan_id))

        def notify_run_start(self, environment: str, *a: t.Any, **k: t.Any) -> None:
            CALLS.append(("run_start", environment, None))

        def notify_run_end(self, environment: str, *a: t.Any, **k: t.Any) -> None:
            CALLS.append(("run_end", environment, None))

        def notify_run_failure(self, exc: str, *a: t.Any, **k: t.Any) -> None:
            CALLS.append(("run_failure", None, None))

    return RecordingTarget


CALLS: t.List[t.Tuple[str, t.Optional[str], t.Optional[str]]] = []
LIFECYCLE = ["apply_start", "apply_end", "apply_failure", "run_start", "run_end", "run_failure"]


@pytest.fixture
def recording_target(probe: Probe) -> t.Iterator[None]:
    """Append the target to ``config.notification_targets`` at load time, as install() will."""
    target_class = _notification_target_class()
    CALLS.clear()

    def hook(config: t.Any) -> t.Any:
        config.notification_targets.append(target_class(notify_on=LIFECYCLE))
        return config

    probe.hook = hook
    yield
    CALLS.clear()


@pytest.mark.usefixtures("recording_target")
def test_notification_target_sees_apply_and_run(project: Path) -> None:
    """sqlmesh/core/context.py apply (~1968) and run (~898) notify around the scoped work."""
    from sqlmesh.core.context import Context

    context = Context(paths=str(project))
    plan = context.plan(auto_apply=True, no_prompts=True, execution_time=PLAN_EXECUTION_TIME)
    context.run(execution_time=RUN_EXECUTION_TIME)

    assert CALLS == [
        ("apply_start", "prod", plan.plan_id),
        ("apply_end", "prod", plan.plan_id),
        ("run_start", "prod", None),
        ("run_end", "prod", None),
    ]


def _failing_project(project: Path) -> None:
    (project / "models" / "events.sql").write_text(
        (project / "models" / "events.sql")
        .read_text()
        .replace(
            "SELECT CAST(@start_ds AS DATE) AS ds, 1 AS id",
            "SELECT CAST(@start_ds AS DATE) AS ds, NULL::INT AS id",
        )
    )


@pytest.mark.usefixtures("recording_target")
def test_notification_target_sees_apply_failure(project: Path) -> None:
    from sqlmesh.core.context import Context
    from sqlmesh.utils.errors import PlanError

    _failing_project(project)
    context = Context(paths=str(project))
    with pytest.raises(PlanError):
        context.plan(auto_apply=True, no_prompts=True, execution_time=PLAN_EXECUTION_TIME)

    assert [call[0] for call in CALLS] == ["apply_start", "apply_failure"]
    assert CALLS[1][1] == "prod"


@pytest.mark.usefixtures("recording_target")
def test_run_with_nothing_to_do_sends_no_end_hook(project: Path) -> None:
    """Edge case found while reading context.py run(): no RUN_END / RUN_FAILURE on nothing-to-do.

    A start event must therefore not be assumed to have a matching end.
    """
    from sqlmesh.core.context import Context

    context = Context(paths=str(project))
    context.plan(auto_apply=True, no_prompts=True, execution_time=PLAN_EXECUTION_TIME)
    CALLS.clear()

    context.run(execution_time=PLAN_EXECUTION_TIME)  # every interval is already done

    assert CALLS == [("run_start", "prod", None)]


@pytest.mark.usefixtures("recording_target")
def test_ui_apply_notifies_with_environment(project: Path) -> None:
    """web/server/api/endpoints/commands.py _run_plan_apply -> Context.apply, on a worker thread."""
    from concurrent.futures import ThreadPoolExecutor

    from web.server import models
    from web.server.api.endpoints.commands import _run_plan_apply
    from web.server.settings import _get_context

    context = _get_context.__wrapped__(str(project), "", None)
    context.load()

    with ThreadPoolExecutor(max_workers=1) as pool:  # run_in_executor uses the default pool
        pool.submit(
            _run_plan_apply,
            context,
            "dev",
            models.PlanOptions(),
            models.PlanDates(start="2024-01-01", end="2024-01-02"),
        ).result()

    assert [(name, env) for name, env, _ in CALLS] == [("apply_start", "dev"), ("apply_end", "dev")]


def test_username_disables_config_notification_targets(project: Path, probe: Probe) -> None:
    """Edge case: NotificationTargetManager.notify only calls user targets when username is set."""
    from sqlmesh.core.config import Config
    from sqlmesh.core.context import Context

    target_class = _notification_target_class()
    CALLS.clear()

    def hook(config: Config) -> Config:
        config.notification_targets.append(target_class(notify_on=LIFECYCLE))
        return config.copy(update={"username": "someone"})

    probe.hook = hook
    context = Context(paths=str(project))
    context.plan(auto_apply=True, no_prompts=True, execution_time=PLAN_EXECUTION_TIME)

    assert CALLS == []


# --- Assumption 4: the nested physical/materialize observation carries target -----------------


def test_materialize_carries_target_inside_model_run(project: Path, probe: Probe) -> None:
    """sqlmesh/core/snapshot/evaluator.py ~800: action("physical", "materialize", target=...)."""
    from sqlmesh.core.context import Context
    from sqlmesh.core.execution_observation import action as _action  # noqa: F401  (pin import)

    recorder = Recorder()
    _wrap_on_config_load(probe, recorder)
    context = Context(paths=str(project))
    context.plan(auto_apply=True, no_prompts=True, execution_time=PLAN_EXECUTION_TIME)

    # Every model start is followed by a materialize finish before the model finish.
    sequence = [(phase, facts["kind"], facts["action"]) for _, phase, facts, _ in recorder.events]
    targets = [facts["target"] for facts in recorder.finished("physical", "materialize")]
    model_runs = recorder.finished("model", "evaluate")

    assert len(targets) == len(model_runs) == 2
    expected_table = context.table_name("obs.events", prod=True)
    assert all(target == expected_table for target in targets), (targets, expected_table)
    for index, (phase, kind, name) in enumerate(sequence):
        if (phase, kind) == ("start", "model"):
            following = sequence[index + 1 :]
            materialize = following.index(("finish", "physical", "materialize"))
            model_finish = following.index(("finish", "model", "evaluate"))
            assert materialize < model_finish


# --- Collector correlation relies on this -------------------------------------------------------


def test_contextvar_set_in_model_start_is_visible_to_nested_work(
    project: Path, probe: Probe
) -> None:
    """The observer runs synchronously in the worker's context (execution_observation._notify), so a
    ContextVar it sets on ``model`` start is visible to the nested materialize and audit events."""
    from contextvars import ContextVar

    from sqlmesh.core.console import get_console
    from sqlmesh.core.context import Context

    current: ContextVar[t.Optional[int]] = ContextVar("current_model", default=None)
    seen: t.List[t.Tuple[str, t.Optional[int]]] = []
    tokens: t.Dict[int, t.Any] = {}
    counter = iter(range(1, 100))

    def observe(phase: str, facts: t.Mapping[str, t.Any], error: t.Any) -> None:
        if facts["kind"] == "model":
            if phase == "start":
                tokens[id(facts)] = current.set(next(counter))
            else:
                current.reset(tokens.pop(id(facts)))
        elif phase == "finish" and facts["kind"] in ("physical", "audit"):
            seen.append((facts["kind"], current.get()))

    def hook(config: t.Any) -> t.Any:
        get_console().get_execution_observer = lambda: observe  # type: ignore[method-assign]
        return config

    probe.hook = hook
    context = Context(paths=str(project))
    context.plan(auto_apply=True, no_prompts=True, execution_time=PLAN_EXECUTION_TIME)

    nested = [value for kind, value in seen if value is not None]
    assert sorted(nested) == [1, 1, 2, 2]  # one materialize + one audit per model run
    assert current.get() is None  # reset on finish leaves the outer context clean


# --- Read-only config loading and state reader -----------------------------------------------


def test_load_configs_without_project_files(tmp_path: Path) -> None:
    """sqlmesh/core/config/loader.py: load_configs resolves a gateway without loading models."""
    from sqlmesh.core.config import Config
    from sqlmesh.core.config.loader import load_configs

    minimal = tmp_path / "minimal"
    minimal.mkdir()
    database = tmp_path / "warehouse.duckdb"
    (minimal / "config.py").write_text(
        f"""
from sqlmesh.core.config import (
    Config,
    DuckDBConnectionConfig,
    GatewayConfig,
    ModelDefaultsConfig,
)

config = Config(
    gateways={{
        "local": GatewayConfig(connection=DuckDBConnectionConfig(database={str(database)!r}))
    }},
    default_gateway="local",
    model_defaults=ModelDefaultsConfig(dialect="duckdb"),
)
"""
    )

    configs = load_configs(None, Config, [minimal])
    assert len(configs) == 1
    loaded = next(iter(configs.values()))
    assert loaded.default_gateway == "local"
    assert loaded.get_connection("local").database == str(database)


def test_state_reader_exposes_environment_owner_and_columns(project: Path) -> None:
    """EngineAdapterStateSync reads environment snapshots, owner, and columns_to_types read-only."""
    from sqlmesh.core.config import Config
    from sqlmesh.core.config.loader import load_configs
    from sqlmesh.core.context import Context
    from sqlmesh.core.state_sync import EngineAdapterStateSync

    (project / "models" / "events.sql").write_text(
        """
MODEL (
  name obs.events,
  owner 'data-platform',
  kind INCREMENTAL_BY_TIME_RANGE (time_column ds, batch_size 1),
  cron '@daily',
  audits (not_null(columns := (id)))
);

SELECT CAST(@start_ds AS DATE) AS ds, 1 AS id
"""
    )
    context = Context(paths=str(project))
    context.plan(auto_apply=True, no_prompts=True, execution_time=PLAN_EXECUTION_TIME)
    context.close()

    loaded = next(iter(load_configs(None, Config, [project]).values()))
    state_conn = loaded.get_state_connection("local") or loaded.get_connection("local")
    adapter = state_conn.create_engine_adapter()
    try:
        reader = EngineAdapterStateSync(adapter, schema=loaded.get_state_schema("local"))
        environment = reader.get_environment("prod")
        assert environment is not None
        assert environment.snapshots
        snapshots = reader.get_snapshots(environment.snapshots)
    finally:
        adapter.close()

    by_name = {snapshot.name: snapshot for snapshot in snapshots.values()}
    events = next(s for name, s in by_name.items() if name.endswith('"events"'))
    assert events.version is not None
    assert events.node.owner == "data-platform"
    columns = events.model.columns_to_types or {}
    assert set(columns) == {"ds", "id"}


# --- physical create / schema_migration carry the snapshot ------------------------------------


def test_physical_create_and_schema_migration_carry_snapshot_lineage(
    project: Path, probe: Probe
) -> None:
    """sqlmesh/core/snapshot/evaluator.py: @observe_snapshot("physical", "create") on
    create_snapshot and @observe_snapshot("physical", "schema_migration") on _migrate_snapshot pass
    the Snapshot. A forward-only change keeps ``version``, gets a new ``identifier``, and lists the
    previous snapshot in ``previous_versions`` (fingerprint.to_identifier() == old identifier).
    """
    from sqlmesh.core.console import get_console
    from sqlmesh.core.context import Context

    seen: t.List[t.Dict[str, t.Any]] = []

    def hook(config: t.Any) -> t.Any:
        console = get_console()

        def get_execution_observer() -> t.Callable[..., None]:
            def observe(phase: str, facts: t.Mapping[str, t.Any], error: t.Any) -> None:
                if phase != "finish" or facts.get("kind") != "physical" or error is not None:
                    return
                if facts.get("action") not in ("create", "schema_migration"):
                    return
                snapshot = facts["snapshot"]
                seen.append(
                    {
                        "action": facts["action"],
                        "identifier": snapshot.identifier,
                        "version": snapshot.version,
                        "previous": [
                            v.fingerprint.to_identifier() for v in snapshot.previous_versions
                        ],
                        "columns": list((snapshot.model.columns_to_types or {}).keys()),
                        "kind": snapshot.model.kind.name.value,
                    }
                )

            return observe

        console.get_execution_observer = get_execution_observer  # type: ignore[method-assign]
        return config

    probe.hook = hook
    context = Context(paths=str(project))
    context.plan(auto_apply=True, no_prompts=True, execution_time=PLAN_EXECUTION_TIME)
    (created,) = [s for s in seen if s["action"] == "create"]
    assert created["columns"] == ["ds", "id"]
    assert created["kind"] == "INCREMENTAL_BY_TIME_RANGE"

    models = project / "models" / "events.sql"
    models.write_text(models.read_text().replace("1 AS id", "1 AS id, 2 AS extra"))
    context.load()
    seen.clear()
    context.plan(
        auto_apply=True, no_prompts=True, forward_only=True, execution_time=PLAN_EXECUTION_TIME
    )
    migrations = [s for s in seen if s["action"] == "schema_migration"]
    assert migrations, seen
    migrated = migrations[-1]
    assert migrated["version"] == created["version"]
    assert migrated["identifier"] != created["identifier"]
    assert created["identifier"] in migrated["previous"]
    assert migrated["columns"] == ["ds", "id", "extra"]
    context.close()
