# sqlmesh-observability

Elementary-style data observability for [SQLMesh](https://github.com/TobikoData/sqlmesh)
projects. It installs as a package next to SQLMesh, is enabled from `config.py`, and needs no
changes to SQLMesh itself.

> **Status: pre-alpha.** Expect breaking changes before 1.0. Requires Python 3.9 or newer and a
> SQLMesh release with the execution-observation API (added after `v0.236.4`).

## What it does

- **Logs every run.** Each plan and run writes its model runs and audit results to an
  `observability` schema in your warehouse, keyed by model version.
- **Gates on anomalies.** `volume_anomaly`, `freshness_anomaly`, and `column_anomaly` are SQLMesh
  audits that compute history from your data and can block a plan before promotion.
- **Watches passively.** It records row counts, freshness lag, column null rates and means, and
  schema changes on every run, and scores them for anomalies when you read them.
- **Reports.** `sqlmesh-observe report` writes an offline HTML dashboard of what is in prod, and
  `sqlmesh-observe serve` serves the same dashboard as a read-only web app.
- **Alerts.** `sqlmesh-observe monitor` sends one grouped message per model owner for new prod
  failures and anomalies, through your own notification target.

## Quick start

```bash
pip install sqlmesh-observability
pip install "sqlmesh-observability[serve]"   # optional: the web app
```

```python
# config.py
from sqlmesh.core.config import Config
from sqlmesh_observability import install

config = install(
    Config(...),
    "warehouse",                                    # gateway that holds the log
    monitors={"volume": True, "schema": True},      # optional passive monitors
)
```

```bash
sqlmesh plan                                # unchanged; the log is written as it runs
sqlmesh-observe report -o report.html       # open the dashboard
sqlmesh-observe init-audits                 # optional: add the anomaly audits to the project
```

See [Getting started](https://w4n2.github.io/sqlmesh-observability/getting-started/) for installation and a full walkthrough.

**Docs site and live demo report:** https://w4n2.github.io/sqlmesh-observability/

## Documentation

| Guide | Covers |
| --- | --- |
| [Getting started](https://w4n2.github.io/sqlmesh-observability/getting-started/) | Requirements, installation, first report |
| [Configuration](https://w4n2.github.io/sqlmesh-observability/configuration/) | Every `install()` argument, and what `install()` changes |
| [CLI reference](https://w4n2.github.io/sqlmesh-observability/cli/) | `init-audits`, `report`, `serve`, `monitor` |
| [Anomaly audits](https://w4n2.github.io/sqlmesh-observability/audits/) | `volume_anomaly`, `freshness_anomaly`, `column_anomaly` |
| [Passive monitors](https://w4n2.github.io/sqlmesh-observability/monitors/) | Volume, freshness, schema, and column metrics, and how they are scored |
| [Report and app](https://w4n2.github.io/sqlmesh-observability/report/) | What the dashboard shows, scope, and deploying `serve` |
| [Alerts](https://w4n2.github.io/sqlmesh-observability/alerts/) | `monitor`, notification targets, grouping, and delivery |
| [Warehouse tables](https://w4n2.github.io/sqlmesh-observability/warehouse-tables/) | The tables the package writes, and their columns |
| [Known limitations](https://w4n2.github.io/sqlmesh-observability/limitations/) | What isn't covered yet |

## How it works

`install()` adds a notification target and wraps the SQLMesh console's execution observer, on
that instance only. It never replaces the console and never writes SQLMesh state. As SQLMesh
works, the observer copies plain facts (model, version, interval, audit result). When each plan
or run finishes, they are written to the package's own tables. The read side (`report`, `serve`,
`monitor`) joins those rows to SQLMesh state, so a version built in dev and promoted to prod
counts as prod, and a dev-only failure never shows up in prod.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
scripts/ci.sh                             # everything a change must pass
```

`scripts/ci.sh` runs, in order:
- the search guards;
- ruff, the format check, and mypy;
- a Python 3.9 syntax check;
- the test suite, against the installed SQLMesh and against SQLMesh `main`.

`tests/test_e2e.py` runs one prod-shaped history through every part of the package.
[docs/verification.md](https://w4n2.github.io/sqlmesh-observability/verification/) maps each SQLMesh behavior the package depends on to
the test that pins it. See [CONTRIBUTING.md](https://github.com/W4N2/sqlmesh-observability/blob/main/CONTRIBUTING.md) for commit rules and
[AGENTS.md](https://github.com/W4N2/sqlmesh-observability/blob/main/AGENTS.md) for guidance aimed at coding agents.

## License

Apache License 2.0. See [LICENSE](https://github.com/W4N2/sqlmesh-observability/blob/main/LICENSE) and [NOTICE](https://github.com/W4N2/sqlmesh-observability/blob/main/NOTICE).
