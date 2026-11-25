# Anomaly audits

The package ships three SQLMesh audits that work like Elementary's `volume_anomalies`,
`freshness_anomalies`, and `column_anomalies` tests. They are ordinary SQLMesh audits, so they
run on every plan and run, and with `blocking := true` they stop a plan before promotion.

Each audit computes its history from the model's own table when it runs. It needs no stored
metrics and works the first time it runs, as long as the table already has enough history.

## Adding them to a project

```bash
sqlmesh-observe init-audits
```

This writes `audits/volume_anomaly.sql`, `audits/freshness_anomaly.sql`,
`audits/column_anomaly.sql`, and `macros/observability_audits.py`. Then attach the audits per
model:

```sql
MODEL (
  name analytics.orders,
  kind INCREMENTAL_BY_TIME_RANGE (time_column order_date),
  audits (
    volume_anomaly(time_column := order_date, source := analytics.orders),
    freshness_anomaly(time_column := updated_at, source := analytics.orders, warn_after_hours := 24),
    column_anomaly(column := status, metric := null_rate, time_column := order_date,
                   source := analytics.orders)
  )
);
```

`source` must name the audited model itself. A named model resolves to the same version's
table with its full history, while `@this_model` would only see the interval being processed.

All three are non-blocking by default. Add `blocking := true` to make a failure stop the plan.

## `volume_anomaly`

Flags a day in the processed interval whose row count is unusual compared with the days before it.

| Argument | Default | Meaning |
| --- | --- | --- |
| `time_column` | required | Column whose date (`CAST(... AS DATE)`) buckets rows into days. |
| `source` | required | The audited model. |
| `training_days` | `14` | Days before each checked day used as history. |
| `min_buckets` | `7` | Fewest history days needed. With fewer, nothing is flagged. |
| `threshold` | `3` | How many sample standard deviations from the history mean count as unusual. |
| `spike_failure_percent_threshold` | off | Flag a day above the mean only if it is also more than this percent above it. |
| `drop_failure_percent_threshold` | off | Flag a day below the mean only if it is also more than this percent below it. |

Days with no rows count as 0, so a missing day is caught.

## `freshness_anomaly`

Fails when the newest `time_column` value is older than the end of the processed interval (no
later than the execution time) minus `warn_after_hours`, or when the model is empty.

| Argument | Default | Meaning |
| --- | --- | --- |
| `time_column` | required | Timestamp column to check. |
| `source` | required | The audited model. |
| `warn_after_hours` | `24` | Allowed age of the newest row. |

## `column_anomaly`

Flags a day whose null rate or average for one column is unusual compared with the days before
it. It takes every `volume_anomaly` argument, plus:

| Argument | Meaning |
| --- | --- |
| `column` | The column to measure. |
| `metric` | `null_rate` (share of rows where the column is null, 0 to 1) or `mean` (average of the non-null values). |

A day with no value is neither flagged nor used as history. That covers a day with no rows, and
a `mean` day where every value is null. Missing data is `volume_anomaly`'s job. An unknown
`metric` is an audit error, and SQLMesh fails the plan on any audit error, even for a
non-blocking audit.

## Things to know

- **Flat history.** If every history day has the same value, the standard deviation is 0 and any
  change is flagged. Elementary does the same. To ignore small changes, set
  `spike_failure_percent_threshold` and/or `drop_failure_percent_threshold`. For example,
  `spike_failure_percent_threshold := 50` flags a high day only if it is more than 50% above the
  mean.
- **Time zones.** Days are bucketed in the warehouse session's time zone. Freshness compares
  timezone-naive timestamps with SQLMesh's execution time, which is UTC.
- **Long backfills.** One batch can cover at most 10,000 days. Set the model's `batch_size` for
  longer backfills.
- **Dev previews.** On dev preview tables built without cloning, SQLMesh makes all audits
  non-blocking, because the table holds only part of the data.
- **Portability.** The audits are written so SQLMesh can transpile them for DuckDB, Snowflake,
  and BigQuery.
