/*
  column_anomaly: flags days whose null rate or mean of one column deviates from recent history.

  Shipped by sqlmesh-observability; copy with `sqlmesh-observe init-audits`, which also copies the
  macros it uses (macros/observability_audits.py).

    audits (
      column_anomaly(column := status, metric := null_rate, time_column := ds,
                     source := analytics.orders),
      column_anomaly(column := amount, metric := mean, time_column := ds,
                     source := analytics.orders)
    )

  column         the column to watch
  metric         null_rate (share of rows where column is null, 0 to 1) or mean (average of the
                 non-null values)
  time_column, source, training_days, min_buckets, threshold,
  spike_failure_percent_threshold, drop_failure_percent_threshold
                 as in volume_anomaly, with the same defaults

  Returns one row per flagged day in the processed interval. A day with no value (no rows, or a
  mean over only nulls) is neither scored nor counted as history: missing data is what
  volume_anomaly reports. min_buckets counts days that have a value.
*/
AUDIT (
  name column_anomaly,
  blocking false,
  defaults (
    training_days = 14,
    min_buckets = 7,
    threshold = 3,
    spike_failure_percent_threshold = NULL,
    drop_failure_percent_threshold = NULL
  )
);

WITH spine AS (
  SELECT bucket
  FROM (@obs_day_spine(@start_ds, @end_ds, @training_days)) AS days
), daily AS (
  SELECT CAST(@time_column AS DATE) AS bucket, @obs_column_metric(@column, @metric) AS metric_value
  FROM @source
  WHERE CAST(@time_column AS DATE) BETWEEN @obs_days_before(@start_ds, @training_days)
    AND CAST(@end_ds AS DATE)
  GROUP BY CAST(@time_column AS DATE)
), series AS (
  SELECT spine.bucket, daily.metric_value
  FROM spine
  LEFT JOIN daily
    ON spine.bucket = daily.bucket
), training AS (
  SELECT
    COUNT(series.metric_value) AS training_buckets,
    AVG(series.metric_value) AS mean_value,
    COALESCE(STDDEV_SAMP(series.metric_value), 0) AS stddev_value
  FROM series
  WHERE series.bucket < CAST(@start_ds AS DATE)
)
SELECT
  series.bucket,
  series.metric_value,
  training.mean_value,
  training.stddev_value,
  (series.metric_value - training.mean_value) / NULLIF(training.stddev_value, 0) AS z_score,
  training.training_buckets
FROM series
CROSS JOIN training
WHERE series.bucket >= CAST(@start_ds AS DATE)
  AND NOT series.metric_value IS NULL
  AND training.training_buckets >= @min_buckets
  AND ABS(series.metric_value - training.mean_value) > @threshold * training.stddev_value
  AND (
    (
      series.metric_value >= training.mean_value
      AND (
        @spike_failure_percent_threshold IS NULL
        OR series.metric_value > (1 + @spike_failure_percent_threshold / 100.0) * training.mean_value
      )
    )
    OR (
      series.metric_value < training.mean_value
      AND (
        @drop_failure_percent_threshold IS NULL
        OR series.metric_value < (1 - @drop_failure_percent_threshold / 100.0) * training.mean_value
      )
    )
  )
