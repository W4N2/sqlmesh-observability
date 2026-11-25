# Getting started

This guide adds observability to an existing SQLMesh project, runs a plan, and opens the report.

## Requirements

- Python 3.9 or newer.
- SQLMesh with the execution-observation API, added after `v0.236.4`. Installing the package
  pulls in a compatible SQLMesh.
- A project configured in Python (`config.py`). The package hooks in from Python config, and
  SQLMesh does not support this from YAML config.

## 1. Install

Install it into the same environment as SQLMesh:

```bash
pip install sqlmesh-observability
```

The web app (`sqlmesh-observe serve`) needs Starlette and uvicorn, which come with the `serve`
extra:

```bash
pip install "sqlmesh-observability[serve]"
```

The package installs the `sqlmesh-observe` command.

## 2. Enable it in `config.py`

Call `install()` last, with the name of the gateway whose warehouse should hold the log:

```python
# config.py
from sqlmesh.core.config import Config, GatewayConfig, ModelDefaultsConfig
from sqlmesh_observability import install

config = install(
    Config(
        gateways={"warehouse": GatewayConfig(...)},
        default_gateway="warehouse",
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
    ),
    "warehouse",
)
```

That's all that's needed to log runs and audits. Every option is described in
[Configuration](configuration.md).

## 3. Run SQLMesh as usual

```bash
sqlmesh plan
sqlmesh run
```

Nothing changes about how you run SQLMesh. When each plan or run finishes, its model runs and
audit results are written to the `observability` schema in that gateway's warehouse. The tables
are created on first use. See [Warehouse tables](warehouse-tables.md).

## 4. Look at the results

```bash
sqlmesh-observe report -o report.html     # one offline HTML file
sqlmesh-observe serve                     # the same report as a live web app
```

Both show what is in `prod` by default: failing audits, anomalies, interval health, and run
history. See [Report and app](report.md).

## Next steps

- Gate deploys on anomalies with the package audits: [Anomaly audits](audits.md).
- Record row counts, freshness, schema changes, and column metrics on every run:
  [Passive monitors](monitors.md).
- Send one grouped message per owner when prod breaks: [Alerts](alerts.md).
