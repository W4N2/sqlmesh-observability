# Configuration

Everything is configured with one call in `config.py`:

```python
from sqlmesh_observability import install

config = install(
    config,                 # your SQLMesh Config
    "warehouse",            # gateway whose warehouse holds the log
    schema="observability",
    monitors={...},         # optional passive monitors
    sample_rows=0,          # failing rows to keep per failed audit
    alert_target=None,      # notification target for `sqlmesh-observe monitor`
)
```

`install()` returns the same `Config` object.

## Arguments

| Argument | Default | Meaning |
| --- | --- | --- |
| `config` | required | The project's SQLMesh `Config`. |
| `connection` | required | Name of a gateway in `config`. Its warehouse holds the observability tables. Nothing connects until the first plan or run finishes. |
| `schema` | `"observability"` | Schema for the package's tables, or `catalog.schema`. |
| `monitors` | off | Passive metrics recorded after each plan or run. See [Passive monitors](monitors.md). |
| `sample_rows` | `0` | When an audit fails, keep up to this many of its failing rows as JSON in `audit_samples`. The audit query is run again on the default gateway to get them. `0` turns this off. |
| `alert_target` | `None` | A notification target instance, usually a `GenericNotificationTarget` subclass, that `sqlmesh-observe monitor` sends through. See [Alerts](alerts.md). |

The read-side commands (`report`, `serve`, `monitor`) load the same `config.py`. When `install()`
is called there, they use its gateway and schema unless you pass `--gateway` or `--schema`.

## What `install()` changes

- It appends one notification target to `config.notification_targets`. That target sends no
  messages. It only marks where each plan or run starts and ends.
- It wraps `get_execution_observer` on the current SQLMesh console instance, so each plan or run
  is observed. It never replaces the console, and it works under `sqlmesh` CLI commands, `sqlmesh
  ui`, and the Python API.
- It never writes SQLMesh state. It owns only its own tables, in its own schema.

Loading `config.py` more than once in a process is safe.

## Install order with other packages

Some packages replace the SQLMesh console from `config.py`. For example, `sqlmesh-openlineage`'s
`install()` swaps in its own console. Call `sqlmesh_observability.install()` after them. If one
runs later anyway, the wrap is re-applied to the new console when the next plan or run starts.

## Failure isolation

Collecting and writing never change the SQLMesh result. An error inside the observer is logged
and ignored. A failed or slow warehouse write is logged, and the plan or run still succeeds or
fails on its own. Writes wait at most 60 seconds when the command finishes. Anything not written
by then is retried when the process exits.
