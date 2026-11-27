# Known limitations

- **SQLMesh version.** The package needs SQLMesh's execution-observation API, added after
  `v0.236.4`. Older SQLMesh versions are not supported.
- **Python config only.** `install()` runs from `config.py`. SQLMesh does not support this from
  YAML config.
- **`sqlmesh audit` is not captured.** It runs outside SQLMesh's observer. An audit-only plan
  stage is captured: its rows have `command = plan`, and its `model_runs` rows have
  `audit_only = true`.
- **`Config.username`.** When it is set, SQLMesh notifies only that user's targets, so
  invocations are captured without an environment or plan id.
- **Concurrent applies in one process.** Two applies running at the same time in one
  `sqlmesh ui` process can be labelled with the wrong environment.
- **A run with nothing to do** gets no end notification from SQLMesh. The next plan or run
  replaces it cleanly. An invocation that starts without a start notification, such as a direct
  `Scheduler` call, may inherit that stale environment.
- **`query_sql` and samples** use the default gateway's dialect.
- **Blocked prod plans** show in the report only while they are the latest prod invocation.
- **JavaScript for detail.** Without JavaScript the report shows only the status, the counts,
  and the open issues. Model pages, test results, runs, and charts are drawn in the browser.
- **History window.** The report keeps each model's latest 30 intervals (`--history`). The
  report and `monitor` still read each in-scope version's full history to score anomalies, and
  there is no date-range picker.
- **Time zones.** Audit day buckets use the warehouse session time zone. Freshness compares
  naive timestamps with SQLMesh's UTC execution time.
- **Lineage is model-level.** There is no column-level lineage, and tables SQLMesh doesn't know
  as models (undeclared raw tables) don't appear. Declare them as external models to see them.
  Lineage needs SQLMesh state; when state can't be read, the Lineage view says so.
- **Alerts:** see [Alerts, things to know](alerts.md#things-to-know).
