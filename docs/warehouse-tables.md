# Warehouse tables

The package writes to its own schema (`observability` by default, set with `install(schema=...)`)
in the warehouse of the gateway passed to `install()`. Tables are created on first use. The
package never writes SQLMesh's own tables or state.

## Tables

| Table | One row per | Written by |
| --- | --- | --- |
| `invocations` | plan or run | every plan and run |
| `model_runs` | model interval batch that was evaluated | every plan and run |
| `audit_results` | audit run on one interval | every plan and run |
| `audit_samples` | stored failing row, as JSON | `sample_rows > 0` |
| `metrics` | metric value per model version and interval | `monitors` (`volume`, `freshness`, `columns`) |
| `schema_columns` | declared column of a snapshot | `monitors={"schema": True}` |
| `schema_changes` | column added, removed, or retyped between a snapshot and its ancestor | `monitors={"schema": True}` |
| `schema_version` | layout version of these tables | the writer |
| `alert_watermarks` | environment that `monitor` has alerted up to | `sqlmesh-observe monitor` only |

## Key columns

**`invocations`:** `invocation_id`, `command` (`plan` or `run`), `environment`, `plan_id`,
`started_at`, `finished_at`, `status` (`success`, `failure`, or `unfinished`), `sqlmesh_version`.

**`model_runs`:** `invocation_id`, `model_name`, `snapshot_id`, `version`, `physical_table`,
`interval_start`, `interval_end`, `batch_index`, `execution_time`, `environment`, `audit_only`,
`status`, `duration_ms`, `error_message`.

**`audit_results`:** `invocation_id`, `model_name`, `snapshot_id`, `version`, `physical_table`,
`environment`, `audit_name`, `args_json`, `interval_start`, `interval_end`, `status` (`pass`,
`fail`, `skip`, or `error`), `violation_count`, `blocking`, `query_sql`, `error_message`.

**`audit_samples`:** `invocation_id`, `model_name`, `audit_name`, `interval_start`, `row_json`.

**`metrics`:** `invocation_id`, `model_name`, `version`, `physical_table`, `interval_start`,
`interval_end`, `metric_name` (`row_count`, `freshness_lag_seconds`, `null_rate`, or `mean`),
`column_name` (`''` for table-level metrics), `metric_value`, `updated_at`, `error_message`.

**`schema_columns`:** `invocation_id`, `model_name`, `snapshot_id`, `version`, `ordinal`,
`column_name`, `data_type`, `recorded_at`.

**`schema_changes`:** `invocation_id`, `model_name`, `from_snapshot_id`, `to_snapshot_id`,
`from_version`, `to_version`, `column_name`, `change_type` (`added`, `removed`, or `retyped`),
`old_type`, `new_type`, `recorded_at`.

**`alert_watermarks`:** `environment`, `watermark_at`, `invocation_id`, `updated_at`.

## How rows relate to environments

Rows are keyed by model **version** and **physical table**, not by environment. `environment`
records where the work ran. A version built in dev and promoted to prod by a virtual update is
prod data, because prod now points at that version's table. The report and `monitor` decide what
is "in prod" when they read, by joining versions to the environment's snapshots in SQLMesh state.

## When rows are written

Rows are written when each plan or run finishes, before the `sqlmesh` command returns, so a
scheduled `sqlmesh run` is saved before its process exits. The wait is capped at 60 seconds, and
anything not written by then is retried when the process exits. Writing the same invocation again
replaces its rows, and a failed write never changes the SQLMesh result.

## Upgrades

`schema_version` holds the table layout version, currently 2. Older logs are upgraded in place on
the next write.
