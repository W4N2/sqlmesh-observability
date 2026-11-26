# AGENTS.md

Guidance for coding agents (and humans) changing this repository. Read it before you edit.

## What this is

`sqlmesh-observability` adds Elementary-style data observability to SQLMesh projects as a
separate Python package:

- a warehouse log of model runs and audit results, keyed by model version;
- package-shipped anomaly audits (`volume_anomaly`, `freshness_anomaly`, `column_anomaly`) that
  can block a plan;
- passive volume, freshness, schema, and column metrics, scored for anomalies at read time;
- a static report, a read-only web app, and grouped alerts (`sqlmesh-observe report`, `serve`,
  `monitor`).

User documentation lives in `docs/` and is published at
https://w4n2.github.io/sqlmesh-observability/. [How it works](docs/how-it-works.md) describes the
architecture.

## Layout

```
src/sqlmesh_observability/
  hook.py        install(): the console wrap and the notification target
  collector.py   observer callbacks -> plain per-invocation records (no I/O)
  writer.py      writer thread: the package's tables, schema version, metrics after commit
  monitors.py    passive metric settings, collection, and read-time scoring
  reader.py      read side: loads config.py without a Context, reads the log and SQLMesh state
  payload.py     the report's data: one compact, versioned JSON payload (status decided here)
  report.py      the page shell and the no-JavaScript fallback; inlines web/
  web/           report.js and report.css: the browser renderer (views, tree, lists, charts)
  textfmt.py     plain-text formatting shared by the report, payload, and alerts
  app.py         `serve`: the read-only Starlette app
  alerts.py      `monitor`: selection, owner grouping, watermark
  cli.py         the `sqlmesh-observe` command
  audits/        SQL audits and macros that `init-audits` copies into a project
tests/           pytest suite on DuckDB; test_e2e.py runs one history through every surface
docs/            user docs (MkDocs); docs/verification.md maps SQLMesh behavior to tests
scripts/         ci.sh (every check), build_demo.py (the site's demo report)
```

## Hard rules

### Extension only

- **Never change SQLMesh.** Use only what released SQLMesh exposes: the
  [execution-observation API](https://github.com/SQLMesh/sqlmesh/blob/main/docs/reference/execution_observations.md),
  notification targets, audits, and the read-only state reader.
- The only patch allowed is the instance-level wrap of `get_console().get_execution_observer`
  inside `install()`. Do not patch SQLMesh classes or module functions, and never call
  `set_console` from package code. `scripts/ci.sh` fails if `src/` contains `set_console` or
  `sqlmesh.materializations`.
- Never write SQLMesh state. Read it only through the state reader.
- The package owns its own tables in its own schema, with its own `schema_version` table for
  upgrades. Upgrades so far only add tables; bump `SCHEMA_VERSION` in `writer.py` when the layout
  changes.

### Observer callbacks

- Callbacks copy plain values and return. Never keep a `Snapshot` or `AuditResult` after the
  matching `finish`, and never open a warehouse connection inside a callback.
- Nothing the package does may change a SQLMesh result. Catch and log errors on the write path.

### Read side

- `report`, `serve`, and `monitor` never create a SQLMesh `Context` and never write, except that
  `monitor` writes its own `alert_watermarks` table.
- What is "in an environment" is decided at read time by joining row versions to the
  environment's snapshots in state. Do not filter by the `environment` column for that.

### Code

- Python 3.9 or newer. Keep syntax 3.9-compatible (`ci.sh` parses every file as 3.9).
- Ruff, line length 100. `mypy src` must pass with `disallow_untyped_defs`.
- Do not import `pandas`, `numpy`, or `duckdb` at module level. Import them inside the function
  that needs them.
- Build SQL with sqlglot expressions, or as SQLMesh audit SQL that transpiles. Package audits must
  work on DuckDB, Snowflake, and BigQuery, and tests check the transpile.
- `starlette` and `uvicorn` are optional (the `serve` extra). Import them only inside `app.py` and
  the `serve` command.

### Tests

- Write a failing test before the change. Tests run on DuckDB, with no Docker and no network.
- `tests/test_sqlmesh_contract.py` pins the SQLMesh behavior the package relies on. If a SQLMesh
  change breaks it, fix the package or the contract test deliberately, never silently.
- Keep `tests/test_e2e.py` passing: the warehouse log, report, app, and alerts must agree on one
  history.
- Report tests check the decoded payload and the HTML fallback. What the browser draws is
  tested by `tests/test_report_browser.py` (Playwright, headless Chromium); a change to
  `web/report.js` needs a browser test, and the suite must pass with no console errors. Payload changes
  bump `SCHEMA_VERSION` in `payload.py` and `report.js` together, and the 1,500-model page must
  stay under the size budget in `tests/test_report_data.py`.

## Commands

```bash
pip install -e ".[dev]"                 # development install
scripts/ci.sh                           # every check a change must pass
pytest tests/test_report.py -q          # one file
python -m playwright install chromium   # once, for tests/test_report_browser.py
python scripts/build_demo.py out.html   # build the demo report locally
pip install -e ".[docs]" && mkdocs serve   # preview the docs site
```

## Documentation

User-visible behavior lives in `docs/`. When you change an option, a command, a table, or what the
report or alerts show, update the matching page in the same change. `mkdocs build --strict` must
pass; it fails on broken links.

## Commits

- Commit only when the maintainer asks.
- Every commit is signed off by its human author with `git commit -s`. Never write a
  `Signed-off-by:` line by hand.
- No AI or tool attribution anywhere: no `Co-Authored-By:` trailer for an assistant and no
  "generated with" lines in commits, pull requests, or tags.
- Never bypass hooks or signing (`--no-verify`, `--no-gpg-sign`), and never rewrite published
  history unless asked.
