/*
  volume_anomaly: flags days whose row count deviates from recent history.

  Shipped by sqlmesh-observability; copy with `sqlmesh-observe init-audits`, which also copies the
  macros it uses (macros/observability_audits.py).

    audits (volume_anomaly(time_column := ds, source := analytics.orders))

  time_column    column whose date defines the daily bucket
  source         the audited model itself. A named model resolves to the same version's physical
                 table, unfiltered; @this_model would only contain the processed interval.
  training_days  days before the processed interval used as history (default 14)
  min_buckets    minimum days of real history, counted from the first day present in source;
                 with fewer, nothing is flagged (default 7)
  threshold      flag when |count - mean| > threshold * sample stddev (default 3). With
                 zero-variance history, any change is flagged.
  spike_failure_percent_threshold
                 ignore spikes smaller than this percent of the training mean: a day above the
                 mean is flagged only if count > (1 + pct / 100) * mean (default NULL, off)
  drop_failure_percent_threshold
                 ignore drops smaller than this percent: a day below the mean is flagged only if
                 count < (1 - pct / 100) * mean (default NULL, off)
                 These two match Elementary's ignore_small_changes. Each applies only in its own
                 direction.

  Returns one row per flagged day in the processed interval. Days with no rows count as 0.
  No dialect is set: the query parses in the project's dialect, and all date arithmetic is done by
  the macros at render time, so the SQL is portable.
*/
AUDIT (
  name volume_anomaly,
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
), history AS (
  SELECT MIN(CAST(@time_column AS DATE)) AS first_bucket
  FROM @source
), counts AS (
  SELECT CAST(@time_column AS DATE) AS bucket, COUNT(*) AS row_count
  FROM @source
  WHERE CAST(@time_column AS DATE) BETWEEN @obs_days_before(@start_ds, @training_days)
    AND CAST(@end_ds AS DATE)
  GROUP BY CAST(@time_column AS DATE)
), series AS (
  SELECT spine.bucket, COALESCE(counts.row_count, 0) AS row_count
  FROM spine
  LEFT JOIN counts
    ON spine.bucket = counts.bucket
), training AS (
  SELECT
    COUNT(*) AS training_buckets,
    AVG(series.row_count) AS mean_row_count,
    COALESCE(STDDEV_SAMP(series.row_count), 0) AS stddev_row_count
  FROM series
  CROSS JOIN history
  WHERE series.bucket < CAST(@start_ds AS DATE)
    AND series.bucket >= history.first_bucket
)
SELECT
  series.bucket,
  series.row_count,
  training.mean_row_count,
  training.stddev_row_count,
  (series.row_count - training.mean_row_count) / NULLIF(training.stddev_row_count, 0) AS z_score,
  training.training_buckets
FROM series
CROSS JOIN training
WHERE series.bucket >= CAST(@start_ds AS DATE)
  AND training.training_buckets >= @min_buckets
  AND ABS(series.row_count - training.mean_row_count) > @threshold * training.stddev_row_count
  AND (
    (
      series.row_count >= training.mean_row_count
      AND (
        @spike_failure_percent_threshold IS NULL
        OR series.row_count > (1 + @spike_failure_percent_threshold / 100.0) * training.mean_row_count
      )
    )
    OR (
      series.row_count < training.mean_row_count
      AND (
        @drop_failure_percent_threshold IS NULL
        OR series.row_count < (1 - @drop_failure_percent_threshold / 100.0) * training.mean_row_count
      )
    )
  )
