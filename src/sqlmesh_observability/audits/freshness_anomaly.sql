/*
  freshness_anomaly: flags a model whose newest data is too old.

  Shipped by sqlmesh-observability; copy with `sqlmesh-observe init-audits`, which also copies the
  macros it uses (macros/observability_audits.py).

    audits (freshness_anomaly(time_column := updated_at, source := analytics.orders, warn_after_hours := 24))

  time_column       timestamp or date column whose maximum is the data's freshness
  source            the audited model itself (unfiltered; see volume_anomaly)
  warn_after_hours  allowed lag in hours (default 24)

  The lag is measured against the end of the processed interval, capped at execution time:
  min(@execution_ts, @end_ts) - warn_after_hours. During a backfill each batch is judged by its own
  interval, so loading history oldest-first does not fail every early batch. In a normal run the
  processed interval ends just before execution time.

  Returns one row (the latest value found, and the cutoff) when the data is stale or the source is
  empty. Times are compared as timezone-naive timestamps; SQLMesh's execution time is UTC.
*/
AUDIT (
  name freshness_anomaly,
  blocking false,
  defaults (
    warn_after_hours = 24
  )
);

SELECT
  latest.max_time,
  @obs_freshness_cutoff(@execution_ts, @end_ts, @warn_after_hours) AS cutoff
FROM (
  SELECT MAX(CAST(@time_column AS TIMESTAMP)) AS max_time
  FROM @source
) AS latest
WHERE latest.max_time IS NULL
  OR latest.max_time < @obs_freshness_cutoff(@execution_ts, @end_ts, @warn_after_hours)
