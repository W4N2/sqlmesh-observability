"""Shared fixtures: a tiny DuckDB SQLMesh project whose ``config.py`` calls back into the test.

The project's ``config.py`` imports a probe module that each test installs in ``sys.modules``.
The probe's ``on_config_load(config)`` runs while SQLMesh loads the config, which is exactly the
moment ``sqlmesh_observability.install()`` runs in a real project.
"""

from __future__ import annotations

import os
import sys
import types
import typing as t
from pathlib import Path

import pytest

os.environ.setdefault("SQLMESH__DISABLE_ANONYMIZED_ANALYTICS", "1")

PROBE_MODULE = "obs_test_probe"

CONFIG_PY = """
import __PROBE__ as probe
from sqlmesh.core.config import (
    Config,
    DuckDBConnectionConfig,
    GatewayConfig,
    ModelDefaultsConfig,
)

config = probe.on_config_load(
    Config(
        gateways={"local": GatewayConfig(connection=DuckDBConnectionConfig(database=__DB__))},
        default_gateway="local",
        model_defaults=ModelDefaultsConfig(dialect="duckdb", start="2024-01-01"),
    )
)
"""

EVENTS_SQL = """
MODEL (
  name obs.events,
  kind INCREMENTAL_BY_TIME_RANGE (time_column ds, batch_size 1),
  cron '@daily',
  audits (not_null(columns := (id)))
);

SELECT CAST(@start_ds AS DATE) AS ds, 1 AS id
"""

# Execution times used across tests: a plan backfills 2024-01-01..02, a later run adds 03..04.
PLAN_EXECUTION_TIME = "2024-01-03"
RUN_EXECUTION_TIME = "2024-01-05"


class Probe(types.ModuleType):
    """Stand-in for the code a project's ``config.py`` runs at load time."""

    def __init__(self) -> None:
        super().__init__(PROBE_MODULE)
        self.loads: t.List[t.Any] = []
        self.hook: t.Callable[[t.Any], t.Any] = lambda config: config

    def on_config_load(self, config: t.Any) -> t.Any:
        from sqlmesh.core.console import get_console

        self.loads.append(get_console())
        return self.hook(config)


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> Probe:
    module = Probe()
    monkeypatch.setitem(sys.modules, PROBE_MODULE, module)
    return module


@pytest.fixture
def project(tmp_path: Path, probe: Probe) -> Path:
    root = tmp_path / "project"
    (root / "models").mkdir(parents=True)
    (root / "config.py").write_text(
        CONFIG_PY.replace("__PROBE__", PROBE_MODULE).replace(
            "__DB__", repr(str(tmp_path / "warehouse.duckdb"))
        )
    )
    (root / "models" / "events.sql").write_text(EVENTS_SQL)
    return root


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The SQLMesh CLI writes ``logs/`` to the working directory; keep it out of the repo."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def restore_console() -> t.Iterator[None]:
    """Tests may install a console (as the CLI and UI do); put the previous one back."""
    from sqlmesh.core import console as console_module

    previous = console_module.get_console()
    yield
    console_module.set_console(previous)
