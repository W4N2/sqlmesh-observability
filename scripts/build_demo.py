"""Build the demo report published with the docs site.

Creates a throwaway DuckDB SQLMesh project, runs a short prod history through it with
sqlmesh-observability installed, and writes the static report:

    python scripts/build_demo.py docs/demo/index.html

The history gives the report something to show:
- 15 days of orders and customers, all passing.
- Day 16: an order volume spike, many null statuses, and a jump in average amount.
- A forward-only change that adds a column to orders (a schema change).
- A new payments model whose blocking audit fails, so prod is not promoted.
"""

from __future__ import annotations

import os
import sys
import tempfile
import typing as t
from pathlib import Path

INCREMENTAL = "INCREMENTAL_BY_TIME_RANGE (time_column ds, batch_size 1, batch_concurrency 1)"
ORDERS = (
    "SELECT CAST(ds AS DATE) AS ds, id, status, amount, updated_at\n"
    "FROM raw.orders WHERE ds BETWEEN @start_ds AND @end_ds"
)
ORDERS_AUDITS = "not_null(columns := (id)), not_null_non_blocking(columns := (status))"

CONFIG = """
from sqlmesh.core.config import Config, DuckDBConnectionConfig, GatewayConfig, ModelDefaultsConfig
from sqlmesh_observability import install

config = install(
    Config(
        gateways={"local": GatewayConfig(connection=DuckDBConnectionConfig(database=__DB__))},
        default_gateway="local",
        model_defaults=ModelDefaultsConfig(dialect="duckdb", start="2024-01-01"),
    ),
    "local",
    monitors={
        "volume": True,
        "freshness": {"column": "updated_at"},
        "schema": True,
        "columns": {"orders": {"null_rate": ["status"], "mean": ["amount"]}},
    },
    sample_rows=3,
)
"""


def _model(root: Path, name: str, body: str, audits: str, owner: str) -> None:
    (root / "models" / f"{name.split('.')[-1]}.sql").write_text(
        f"MODEL (\n  name {name},\n  owner '{owner}',\n  kind {INCREMENTAL},\n"
        f"  cron '@daily',\n  audits ({audits})\n);\n\n{body}\n"
    )


def build(root: Path, output: Path) -> Path:
    from sqlmesh.core.context import Context

    from sqlmesh_observability.reader import load_report_data
    from sqlmesh_observability.report import write_report

    (root / "models").mkdir(parents=True)
    (root / "config.py").write_text(CONFIG.replace("__DB__", repr(str(root / "demo.duckdb"))))
    _model(root, "analytics.orders", ORDERS, ORDERS_AUDITS, "commerce-team")
    _model(
        root,
        "analytics.customers",
        "SELECT CAST(@start_ds AS DATE) AS ds, 1 AS customer_id, 'alice' AS name",
        "not_null(columns := (customer_id)), unique_values(columns := (customer_id))",
        "crm-team",
    )

    context = Context(paths=str(root))
    adapter = context.engine_adapter
    adapter.execute("CREATE SCHEMA IF NOT EXISTS raw")
    adapter.execute(
        "CREATE TABLE raw.orders "
        "(ds DATE, id INT, status TEXT, amount DOUBLE, updated_at TIMESTAMP)"
    )

    def day(n: int, rows: int = 100, null_status: int = 0, amount: float = 20.0) -> None:
        adapter.execute(
            f"INSERT INTO raw.orders SELECT DATE '2024-01-{n:02d}', i, "
            f"CASE WHEN i < {null_status} THEN NULL ELSE 'paid' END, {amount} + (i % 7), "
            f"TIMESTAMP '2024-01-{n:02d} 18:00:00' FROM range({rows + (n * 13) % 9}) t(i)"
        )

    def plan(**kwargs: t.Any) -> None:
        context.load()
        context.plan("prod", auto_apply=True, no_prompts=True, **kwargs)

    for n in range(1, 16):
        day(n)
    plan(execution_time="2024-01-16")

    day(16, rows=480, null_status=300, amount=95.0)
    context.run(execution_time="2024-01-17")

    _model(
        root,
        "analytics.orders",
        ORDERS.replace("updated_at\n", "updated_at, 'web' AS channel\n"),
        ORDERS_AUDITS,
        "commerce-team",
    )
    plan(forward_only=True, execution_time="2024-01-17")

    _model(
        root,
        "analytics.payments",
        "SELECT CAST(@start_ds AS DATE) AS ds, "
        "CASE WHEN i % 4 = 0 THEN NULL ELSE i END AS payment_id, i * 1.5 AS amount "
        "FROM range(8) t(i)",
        "not_null(columns := (payment_id))",
        "payments-team",
    )
    try:
        plan(execution_time="2024-01-17")
    except Exception as error:  # the blocking audit stops the plan, as intended
        print(f"expected blocking failure: {type(error).__name__}")
    context.close()

    data = load_report_data(root)
    if not data.failing_models or not data.anomalies:
        raise SystemExit("demo history did not produce the expected failures and anomalies")
    return write_report(data, output)


def main(argv: t.Sequence[str]) -> int:
    if len(argv) != 1:
        print("usage: python scripts/build_demo.py OUTPUT.html", file=sys.stderr)
        return 2
    output = Path(argv[0]).resolve()
    previous = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="sqlmesh-observe-demo-") as tmp:
        # Reading SQLMesh state writes a small cache into the working directory; keep it here.
        os.chdir(tmp)
        try:
            written = build(Path(tmp) / "project", output)
        finally:
            os.chdir(previous)
    print(f"wrote {written}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
