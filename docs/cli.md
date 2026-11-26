# CLI reference

The package installs one command, `sqlmesh-observe`, with four subcommands.

| Command | What it does |
| --- | --- |
| [`init-audits`](#init-audits) | Copy the package audits into a project. |
| [`report`](#report) | Write the report as one offline HTML file. |
| [`serve`](#serve) | Serve the report as a read-only web app. |
| [`monitor`](#monitor) | Send one grouped message per owner for new issues. |

## Shared options

`report`, `serve`, and `monitor` read the warehouse log and SQLMesh state. They take the same
options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--path` | `.` | Directory containing `config.py`. Model files are not needed, and no SQLMesh `Context` is created. |
| `--gateway` | the gateway given to `install()`, else `default_gateway` | Gateway holding the observability tables. |
| `--schema` | the schema given to `install()`, else `observability` | Schema holding the observability tables. |
| `--environment` | `prod` | Environment to show. |

SQLMesh state is read from the project's default gateway first, then from `--gateway`. None of
these commands write to SQLMesh state. Only `monitor` writes to the warehouse, and only to its
`alert_watermarks` table.

## `init-audits`

```bash
sqlmesh-observe init-audits [--path PATH] [--force]
```

Copies `volume_anomaly.sql`, `freshness_anomaly.sql`, and `column_anomaly.sql` into
`<path>/audits/`, and the macros they call into `<path>/macros/observability_audits.py`. It lists
each file it writes, or prints `audits are up to date`.

If a target file exists with different contents, for example because you edited it, the command
stops with exit code 1. `--force` overwrites it. Run `init-audits --force` after upgrading the
package to pick up new or changed audits.

## `report`

```bash
sqlmesh-observe report [shared options] [-o OUTPUT] [--history N]
```

Writes the report to `OUTPUT` (default `report.html`), creating parent directories if needed.
`--history` sets how many of each model's latest intervals the page keeps (default 30); see
[History window](report.md#history-window).
Exits 1 if the schema has no `audit_results` table. If SQLMesh state for the environment cannot be
read, the report still writes, prints a warning on stderr, and shows only the latest invocation
that targeted the environment. See [Report and app](report.md).

## `serve`

```bash
sqlmesh-observe serve [shared options] [--host HOST] [--port PORT] [--history N]
```

Runs the read-only web app with uvicorn. The defaults are `--host 127.0.0.1`, `--port 8000`, and
`--history 30`.
It needs the `serve` extra (`pip install "sqlmesh-observability[serve]"`). Without it, the command
exits 1 and says so. See [Report and app](report.md#web-app).

## `monitor`

```bash
sqlmesh-observe monitor [shared options]
```

Sends one message per model owner for audit failures and anomalies that are new since the last
successful run, through the `alert_target` given to `install()`. With no target, it prints the
messages instead. It prints `no new issues in <environment>` when there is nothing to send.

Exit codes: `0` when everything was sent, or nothing was new. `1` when a send raised, in which
case the watermark is left where it was, or when the schema has no `audit_results` table. See
[Alerts](alerts.md).
