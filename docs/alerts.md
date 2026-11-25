# Alerts

`sqlmesh-observe monitor` sends **one message per model owner** for new prod audit failures and
anomalies, through a notification target you provide. Schedule it to run every few minutes.

## Setup

Write a SQLMesh `GenericNotificationTarget` subclass and pass an instance to `install()`:

```python
# config.py
from sqlmesh.core.notification_target import GenericNotificationTarget
from sqlmesh_observability import install

class TeamChat(GenericNotificationTarget):
    def send(self, notification_status, msg, **kwargs):
        post_to_chat(msg)

config = install(Config(...), "warehouse", alert_target=TeamChat())
```

```bash
sqlmesh-observe monitor            # e.g. every 10 minutes from cron or your orchestrator
```

Without an `alert_target`, `monitor` prints the messages instead.

## What gets sent

`monitor` collects failed or errored audits and anomalous metric points that match all three of
these:
1. **In the environment's view:** `prod` by default, chosen the same way as for the
   [report](report.md#whats-in-scope).
2. **Produced by a plan or run that targeted that environment.** A dev build's failures are left
   out, even after the version is promoted.
3. **Newer than the watermark:** finished after the newest invocation already alerted on.

They are grouped by model owner, read from SQLMesh state. Models without an owner share one
message. A message looks like this:

```
sqlmesh-observe: 2 new issues in prod for payments-team
- db.analytics.payments · not_null(columns := (payment_id)) · 2024-01-01 · blocking failure · 2 rows
- db.analytics.orders · row_count anomaly · 2024-01-16 · anomaly · 481 (expected 96.47 – 112.38)
```

## What your target receives

`monitor` calls `send()` on your target directly, once per group. It does not go through
`notify_on`. The call is:

```python
target.send(notification_status, msg, owner=..., alerts=[...])
```

| Argument | Value |
| --- | --- |
| `notification_status` | `NotificationStatus.FAILURE` if the group has a blocking failure or an audit error, otherwise `NotificationStatus.WARNING`. |
| `msg` | The message text shown above. |
| `owner` | The owner, or `None` for the group without one. |
| `alerts` | One dict per line: `kind` (`audit` or `anomaly`), `severity`, `model_name`, `owner`, `signal`, `interval_start`, `status`, `detail`, `invocation_id`. |

## Delivery

The watermark is stored per environment in the `alert_watermarks` table. It moves forward only
after every group's `send()` returns. If any send raises an exception, `monitor`:
- exits 1;
- leaves the watermark where it was;
- sends the same messages again on the next run.

A group that was already delivered can therefore arrive twice, so delivery is at least once.

## Things to know

- **First run.** With no watermark yet, the first run alerts on every failure and anomaly
  already in the log.
- **Double notification.** `alert_target` is used only by `monitor`, and it is not added to your
  project's notification targets. SQLMesh's own `audit_failure` notification still fires during
  plans and runs. A target that has `audit_failure` in `notify_on` and also receives `monitor`
  messages is told twice. Remove `audit_failure` from `notify_on` if `monitor` is your alert path.
- **Promotions.** A version built in dev and then promoted is never alerted on, because its audit
  results came from a dev run. Its earlier prod failures were already alerted on if `monitor` ran
  while that version was current, so schedule `monitor` regularly.
- **Close finishes.** If two plans or runs finish within moments of each other, a `monitor` run
  that falls between their writes can step past the earlier one.
