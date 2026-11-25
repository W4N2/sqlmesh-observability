# Verification checklist

For contributors. Each line is a SQLMesh behavior this package depends on. For each one, the
table names the SQLMesh doc that states it (paths are relative to the SQLMesh repository),
the package code that relies on it, and the test that pins it. `scripts/ci.sh` runs every test
named here on the installed SQLMesh and on SQLMesh `main`.

| Claim | SQLMesh doc | Package code | Pinned by |
| --- | --- | --- | --- |
| Observer exceptions do not change the run. SQLMesh suppresses ordinary `Exception`s from observer callbacks, and the package also catches its own failures before they reach SQLMesh. | `docs/reference/execution_observations.md` ("Callback lifecycle") | `hook.wrap_console`: every call into the chained observer and the collector is wrapped in `try/except Exception`. The writer runs on its own thread, and its errors are logged. | `tests/test_install.py::test_collector_error_is_isolated`, `tests/test_warehouse_log.py::test_writer_failure_does_not_fail_the_run` |
| Incremental audit rows are interval-scoped: an audit runs once per processed interval, over that interval's rows. | `docs/concepts/audits.md` | `collector.py` records `interval_start`/`interval_end` on every `audit_results` row. `reader.audit_key` includes the interval. | `tests/test_warehouse_log.py::test_plan_writes_pass` (two intervals, two rows) |
| A plan whose blocking audit fails does not promote. | `docs/concepts/audits.md` ("Plan vs. Run") | The reader shows an unpromoted failure through the latest invocation that targeted the environment, not through the environment's snapshots. | `tests/test_warehouse_log.py::test_plan_writes_blocking_fail` (`get_environment("prod") is None`), `tests/test_report.py::test_reader_blocking_fail_and_pass_with_samples_and_state_metadata`, `tests/test_e2e.py::test_e2e_prod_report_and_alert` (dev) |
| The package is enabled from `config.py`, not from YAML: custom Python hooks need the Python configuration format. | `docs/guides/customizing_sqlmesh.md` ("Python configuration only") | `install(config, ...)` is called in `config.py`. It returns the same `Config`, and there is no YAML key. | `tests/test_install.py::test_install_returns_same_config`, `tests/test_sqlmesh_contract.py::test_config_loads_before_console_capture` |
| `sqlmesh.materializations` is unused. The package adds no custom materialization. | `docs/guides/custom_materializations.md` | None. | `scripts/ci.sh` guard: no `sqlmesh.materializations` in `src/` |
| The package never replaces the console. Its only patch is the instance-level wrap of `get_execution_observer`. | `docs/reference/execution_observations.md` | `hook.wrap_console` | `tests/test_install.py::test_install_keeps_api_console`, `scripts/ci.sh` guard: no `set_console` in `src/` |
| The warehouse log, report, app, and alerts agree on one prod history, and a dev-only failure appears in none of them. | `docs/concepts/plans.md` ("Virtual Update") | `reader.load_report_data_for`, `app.create_app`, `alerts.run_monitor` | `tests/test_e2e.py::test_e2e_prod_report_and_alert` |
