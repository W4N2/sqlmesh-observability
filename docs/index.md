# sqlmesh-observability

Elementary-style data observability for [SQLMesh](https://github.com/SQLMesh/sqlmesh) projects.
It installs as a Python package next to SQLMesh, is enabled from `config.py`, and needs no changes
to SQLMesh itself.

!!! warning "Pre-alpha"
    Expect breaking changes before 1.0. Requires Python 3.9 or newer and a SQLMesh release with
    the execution-observation API (added after `v0.236.4`).

[Open the live demo report](demo/index.html){ .md-button .md-button--primary }
[Get started](getting-started.md){ .md-button }

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

## The demo

The [demo report](demo/index.html) is rebuilt from the current code on every change to `main`.
`scripts/build_demo.py` runs a small DuckDB project through a short prod history:

1. Fifteen days of orders and customers, all passing.
2. Day 16 brings an order volume spike, 300 null statuses, and a jump in average amount. The
   passive monitors flag all three as anomalies, and a non-blocking `not_null` audit fails.
3. A forward-only change adds a `channel` column to orders. The schema monitor records it.
4. A new `payments` model fails its blocking `not_null` audit, so prod is not promoted. The
   report still shows the failure, with sample rows.

## Quick start

```bash
pip install sqlmesh-observability
```

```python
# config.py
from sqlmesh.core.config import Config
from sqlmesh_observability import install

config = install(Config(...), "warehouse", monitors={"volume": True, "schema": True})
```

```bash
sqlmesh plan                                # unchanged; the log is written as it runs
sqlmesh-observe report -o report.html       # open the dashboard
```
