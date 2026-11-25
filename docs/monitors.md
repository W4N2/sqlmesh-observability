# Passive monitors

Passive monitors record a metric for every interval a plan or run builds. They never block a
deploy: anomalies are scored when you read the report or run `monitor`, not when SQLMesh runs. To
stop a deploy on an anomaly, use the [anomaly audits](audits.md).

They are off unless you pass `monitors` to `install()`:

```python
config = install(
    config,
    "warehouse",
    monitors={
        "volume": True,
        "freshness": {"column": "updated_at", "columns": {"analytics.orders": "order_ts"}},
        "schema": True,
        "columns": {"orders": {"null_rate": ["status"], "mean": ["amount"]}},
    },
)
```

Unknown keys are ignored with a warning.

## `volume`

`COUNT(*)` for each interval that was built, using SQLMesh's own interval filter on the physical
table. For a model without a time column, it counts the whole table. VIEW, EMBEDDED, and EXTERNAL
models are skipped. Stored as metric `row_count`.

## `freshness`

`execution_time - MAX(column)` in seconds, over the whole table. Stored as metric
`freshness_lag_seconds`.

| Key | Meaning |
| --- | --- |
| `column` | Timestamp column to check in every model that has it. |
| `columns` | Per-model overrides, `{"<model>": "<column>"}`. |

Models that have neither column are skipped.

## `schema`

The declared columns of every snapshot SQLMesh creates or migrates, and how they differ from the
snapshot's ancestor: columns `added`, `removed`, or `retyped`. It includes forward-only changes,
which keep the same version. Stored in `schema_columns` and `schema_changes`.

## `columns`

`null_rate` and `mean` for listed columns only. Nothing else is scanned.

```python
monitors={"columns": {"orders": {"null_rate": ["status"], "mean": ["amount"]}}}
```

- **`null_rate`:** the share of rows in the interval where the column is null, from 0 to 1.
- **`mean`:** the average of the column's non-null values in the interval.

All listed columns of a model are measured in one query per interval. An empty interval, or a
`mean` over only nulls, stores no value. A listed column the model doesn't have gets a row with
an error message, and the other columns are still measured.

Model keys match by name suffix, so `"orders"` matches `analytics.orders`. If two models share
the last part of their name, use the longer name. The same rule applies to the `freshness`
`columns` overrides.

## Anomaly scoring

Every metric series is scored with the `volume_anomaly` rule when it is read. Each point is
compared with the points in the `training_days` before it, and flagged when
`|value - mean| > threshold * stddev`.

`volume` and `freshness` accept the scoring settings as a dict in place of `True`:

```python
monitors={"volume": {"training_days": 28, "threshold": 4, "spike_failure_percent_threshold": 50}}
```

| Setting | Default |
| --- | --- |
| `training_days` | `14` |
| `min_buckets` | `7` |
| `threshold` | `3` |
| `spike_failure_percent_threshold` | off |
| `drop_failure_percent_threshold` | off |

Column metrics use the defaults. On a flat history, any change is flagged unless a percent
threshold is set.

## When metrics run

Metrics are measured after the plan's or run's own rows are committed, on the package's writer
thread. They are only taken for intervals that succeeded, so an interval stopped by a blocking
audit gets none. A failing or slow metric query loses only that metric and never changes the
SQLMesh result. Re-running or restating an interval replaces its value.

Like audit results, metrics belong to a model version, not an environment. A version built in dev
and then promoted to prod is part of the prod series.
