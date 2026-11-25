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

The page is laid out as a console:
- a left rail with the views and the model list, where you can search and filter by status or
  owner;
- a top bar with the light/dark switch, which remembers your choice and follows the system until
  you pick one;
- the work area in the middle;
- a run log of recent plans and runs on the right.

Color only ever means state: red for blocking failures and errors, amber for anomalies and
warnings, green for passing.

| View | Shows |
| --- | --- |
| Dashboard | Counts that link to their rows. An Incidents panel led by the most urgent item, then blocking failures, latest-interval anomalies, and non-blocking failures. Interval health for every model. |
| Incidents | Every failed audit with its arguments, interval, snapshot, version, rendered SQL, and stored sample rows. |
| Test results | Every audit result in scope. |
| Model runs | Every plan and run, with counts and durations. |
| Schema changes | Every recorded column change. |
| Models | An index, then a page per model: owner, columns, version, interval health, run duration, a chart per metric with its expected band and anomaly markers, schema changes, and audit history. |

Interval health marks each interval with the worst outcome of its latest run: `P` pass, `W`
non-blocking failure, `F` blocking failure, `E` error, `S` skipped.

Owners and column types come from SQLMesh state. That includes snapshots that ran but were not
promoted.

## Static report

```bash
sqlmesh-observe report -o report.html
```

The file is self-contained. It loads nothing from the network and holds no warehouse credentials.
Without JavaScript, every view is shown in order on one page. With JavaScript, one view shows at a
time. Options are listed in the [CLI reference](cli.md#report).

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

## Things to know

- A prod plan stopped by a blocking audit shows only while it is the latest prod invocation. The
  next `sqlmesh run` against prod replaces it.
- Audit rows are filtered in SQL to the environment's versions, but each version's full history
  is read. There is no time window yet, and long histories make wide charts.
