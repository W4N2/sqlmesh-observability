# Report and app

The report shows what is deployed in one environment, `prod` by default: failing audits,
anomalies, interval health, metric charts, schema changes, and run history. You can get it two
ways, from the same data and the same page:

- `sqlmesh-observe report` writes it as one offline HTML file.
- `sqlmesh-observe serve` serves it live and reads fresh data on every request.

## What's in scope

Rows are matched to the environment by model **version**, not by the environment they ran in. The
report joins each row's version to the environment's snapshots in SQLMesh state. That has two
effects:

- A version built in dev and promoted to prod with a virtual update shows in prod.
- A dev-only version never shows in prod.

The latest invocation that targeted the environment is always included as well. That way, a prod
plan stopped by a blocking audit still shows, even though nothing was promoted.

Use `--environment dev` to check a dev environment before promoting it.

If SQLMesh state for the environment cannot be read, or the environment doesn't exist yet, the
report says so in a banner and shows only the latest invocation that targeted it.

## Layout

The page is built to stay usable at 1,500 models and more:

- A top bar with the views, a **Jump to model** search (press `/`), the last plan or run, and
  the light/dark switch. The page follows the system theme until you pick one, then remembers
  your choice.
- A model tree on the left, grouped by schema or by owner, with healthy, warning, and failing
  counts on each group. Groups with a failing model start expanded. Type in the filter box to
  narrow the tree.
- The view itself in the middle.

Color only ever means state: red for blocking failures and errors, amber for anomalies and
warnings, green for passing. Every colored mark also has a text label.

| View | Shows |
| --- | --- |
| Dashboard | The environment's status, three summaries (audit results, model health, monitored models), one count per signal split into pass, warn, and fail, daily trends, and the open issues: blocking failures and errors, then latest-interval anomalies, then non-blocking failures. |
| Models | Every model with its status, owner, recent interval health, failing audits, anomalies, downstream count, and latest interval. |
| Model page | Owner, version, downstream count, interval health, a chart for run duration and for each metric with its expected band and anomaly markers, its direct upstream and downstream models, columns, schema changes, and audit history. |
| Lineage | One model's upstream and downstream models as a graph, or the Impact list of models with open issues by how many models they affect. See [Lineage](#lineage). |
| Test results | Each audit's latest interval by default, or every interval. |
| Incidents | Every failed audit in scope. |
| Runs | Recent plans and runs, with counts and durations. |
| Schema changes | Every recorded column change. |

Every list has its filters in one row above it, sorts by any column, and draws only the rows on
screen, so tens of thousands of rows stay fast. Click a row in the open issues, test results,
or incidents to expand it in place: you get the audit's arguments, interval, snapshot, version,
sample rows, the audit query, and, for an anomaly audit or a metric, its chart.

Charts show dates on the x-axis, the expected band, and anomaly markers. Hover a chart, or
focus it and use the arrow keys, to read each point. **Table** switches a chart to its values.

Interval health marks each interval with the worst outcome of its latest run: pass,
non-blocking fail, blocking fail or error, or skipped.

Owners and column types come from SQLMesh state. That includes snapshots that ran but were not
promoted.

Filters are kept in the page address, so a link such as
`report.html#models?status=fail&owner=team-03` opens with them applied.

## Lineage

Lineage comes from SQLMesh state: each snapshot in the environment records the models it reads.
That includes external models declared in the project and new models whose plan was stopped
before promotion. The report never loads your model files to get it.

The **Graph** shows one model with its upstream models to the left and downstream models to the
right. It opens on the model whose open issue affects the most downstream models. Click any
model to centre the graph on it.

- **Depth** (1, 2, 3, or all) sets how many levels to follow each way. The default is 2.
- **Direction** shows upstream, downstream, or both.
- **Issues only** keeps the chosen model and the models with failures or anomalies, and folds
  the healthy models between them into one "N healthy models" step.

Each model's card shows its status, schema, kind, and owner, how many models are downstream of
it, and the names of its failed audits and anomalous metrics (or its latest interval when
healthy). External models have a dashed outline. A graph draws at most 150
models, and at most 20 in one column; the rest of a column folds into "+N more", which lists
them when clicked.

**Impact** lists every model with an open issue, sorted by how many models depend on it,
directly or further down. Expand a row to see those models. The open issues on the dashboard
and the models list show the same downstream count.

Without JavaScript, each open issue lists its direct downstream models.

## History window

Each model's interval health, run durations, audit history, and metric charts keep the latest
30 intervals. Change it with `--history`:

```bash
sqlmesh-observe report --history 60
```

Anomalies are still scored against each metric's full history; only what the page stores is
cut. Open issues and their incidents are always kept, however old.

## Static report

```bash
sqlmesh-observe report -o report.html
```

The file is self-contained. It loads nothing from the network and holds no warehouse credentials.
The report's data is embedded in the page as JSON, and the page draws every view from it in the
browser. A report for 1,500 models with the default history is under 3 MB.

Without JavaScript, the page shows the environment's status, the counts, and every open issue.
Model pages, test results, runs, and charts need JavaScript. Options are listed in the
[CLI reference](cli.md#report).

## Web app

```bash
pip install "sqlmesh-observability[serve]"
sqlmesh-observe serve --host 0.0.0.0 --port 8000
```

| Route | Opens on |
| --- | --- |
| `GET /` | The dashboard. |
| `GET /models/{name}` | That model's page. `name` can be the full name, the name without quotes, or a unique suffix such as `analytics.orders`. 404 if nothing matches. |
| `GET /incidents/{invocation_id}/{audit_name}` | The newest matching failed audit. 404 if nothing matches. |
| `GET /data.json` | The report's data as JSON, exactly as embedded in the page. |

The app is read-only. No route runs a model, applies a plan, loads a SQLMesh `Context`, or writes
anything. Every method other than GET and HEAD returns 405. If the schema has no `audit_results`
table, pages return 503.

Requests are handled one at a time. Each one reads the log and SQLMesh state again.

### Deploying

- **Credentials** come from `config.py`, or the environment variables it reads, exactly as they
  do for `sqlmesh`.
- **Database user:** give the app one that can only `SELECT` from the observability schema and
  the SQLMesh state schema.
- **Authentication:** the app has none. Run it behind your own proxy.
- **DuckDB:** while a plan holds the database file, requests can fail until it finishes.
- **Local cache:** reading snapshots makes SQLMesh write a small cache into the working directory
  (`optimized_query/`, `snapshot/`). Start the app from a directory where that's acceptable.

## The data format

The JSON embedded in the page (and served at `/data.json`) carries a `schema_version`, currently
`2` (version 2 added lineage). A page that meets a version it doesn't know shows only the no-JavaScript summary. The fields
are documented in `src/sqlmesh_observability/payload.py`; they may change before 1.0, and any
change bumps `schema_version`.

## Things to know

- A prod plan stopped by a blocking audit shows only while it is the latest prod invocation. The
  next `sqlmesh run` against prod replaces it.
- Audit rows are filtered in SQL to the environment's versions, and each version's full history
  is read. The [history window](#history-window) only limits what the page stores.
