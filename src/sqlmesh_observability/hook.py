"""``install()``: the only code in the package that touches SQLMesh objects directly.

It makes exactly one patch: an instance attribute ``get_execution_observer`` on the console that
is current while ``config.py`` loads. It never replaces the process console and never patches a
class or a module function.
"""

from __future__ import annotations

import logging
import typing as t

from sqlmesh.core.notification_target import (
    GenericNotificationTarget,
    NotificationEvent,
    NotificationStatus,
)

from sqlmesh_observability.collector import get_collector
from sqlmesh_observability.monitors import MonitorSettings, normalize_monitors

if t.TYPE_CHECKING:
    from sqlmesh.core.config import Config

logger = logging.getLogger(__name__)

_MARKER = "__sqlmesh_observability_wrapped__"

_LIFECYCLE_EVENTS = frozenset(
    {
        NotificationEvent.APPLY_START,
        NotificationEvent.APPLY_END,
        NotificationEvent.APPLY_FAILURE,
        NotificationEvent.RUN_START,
        NotificationEvent.RUN_END,
        NotificationEvent.RUN_FAILURE,
    }
)


def install(
    config: "Config",
    connection: t.Any,
    schema: str = "observability",
    monitors: t.Optional[t.Dict[str, t.Any]] = None,
    sample_rows: int = 0,
    alert_target: t.Optional[t.Any] = None,
) -> "Config":
    """Enable observability for the SQLMesh project whose ``config.py`` calls this.

    Call it last in ``config.py``, after any other package that replaces the console::

        config = install(Config(...), "warehouse")

    Args:
        config: The project's SQLMesh ``Config``. It is returned unchanged except for one appended
            notification target.
        connection: Name of the gateway in ``config`` whose warehouse holds the observability
            tables. Nothing connects until the first plan or run finishes.
        schema: Schema (or ``catalog.schema``) for the package's own tables.
        monitors: Passive monitors to run after each plan or run, for example
            ``{"volume": True, "freshness": {"column": "updated_at"}, "schema": True}``, plus
            ``"columns": {"orders": {"null_rate": ["status"], "mean": ["amount"]}}`` for named
            columns. Off when omitted. ``volume`` and ``freshness`` also take the anomaly
            scoring settings (``training_days``, ``min_buckets``, ``threshold``,
            ``spike_failure_percent_threshold``, ``drop_failure_percent_threshold``).
        sample_rows: For a failing audit, store up to this many of its rows as JSON in
            ``audit_samples``, by re-running the audit query on the default gateway. 0 is off.
        alert_target: A notification target instance (a ``GenericNotificationTarget`` subclass)
            that ``sqlmesh-observe monitor`` sends its grouped messages through. Only ``monitor``
            uses it: it is not added to ``config.notification_targets``, so SQLMesh never calls
            it during a plan or run.

    Returns:
        The same ``config`` object.
    """
    from sqlmesh.core.console import get_console

    collector = get_collector()
    collector.settings = {
        "connection": connection,
        "schema": schema,
        "monitors": monitors,
        "sample_rows": sample_rows,
        "alert_target": alert_target,
    }
    _configure_writer(
        collector, config, connection, schema, sample_rows, normalize_monitors(monitors)
    )
    wrap_console(get_console())
    if not any(isinstance(target, ObservabilityTarget) for target in config.notification_targets):
        config.notification_targets.append(ObservabilityTarget())
    return config


def _configure_writer(
    collector: t.Any,
    config: "Config",
    gateway: t.Any,
    schema: str,
    sample_rows: int,
    monitors: "MonitorSettings",
) -> None:
    """Point the collector at a writer for ``gateway``. Re-loading ``config.py`` reuses it."""
    from sqlmesh_observability.writer import Writer

    try:
        collector.dialect = config.get_connection().DIALECT
    except Exception:
        collector.dialect = None
    if not isinstance(gateway, str):
        logger.warning("sqlmesh-observability: `connection` must be a gateway name; not writing")
        collector.writer = None
        return
    writer = collector.writer
    if writer is not None and writer.settings() == (gateway, schema, sample_rows, monitors):
        writer.config = config  # the newest load of config.py wins
        return
    collector.writer = Writer(
        config, gateway, schema=schema, sample_rows=sample_rows, monitors=monitors
    )


def is_wrapped(console: t.Any) -> bool:
    """True when ``console.get_execution_observer`` already routes to this package.

    Reads the attribute, so it also sees through delegating wrappers that forward attribute access
    to a console this package already wrapped.
    """
    try:
        return bool(getattr(console.get_execution_observer, _MARKER, False))
    except Exception:
        return False


def wrap_console(console: t.Any) -> bool:
    """Wrap ``get_execution_observer`` on this console instance. False if already wrapped."""
    if is_wrapped(console):
        return False
    try:
        previous_getter = console.get_execution_observer
    except Exception:
        logger.debug("sqlmesh-observability: console has no execution observer getter")
        return False

    def get_execution_observer() -> t.Optional[t.Callable[..., None]]:
        previous = None
        try:
            previous = previous_getter()
        except Exception:
            logger.debug("sqlmesh-observability: chained observer getter failed", exc_info=True)

        try:
            invocation_id: t.Optional[str] = get_collector().begin()
        except Exception:
            logger.debug("sqlmesh-observability: could not start invocation", exc_info=True)
            invocation_id = None
        if invocation_id is None:
            return previous

        def observe(
            phase: t.Literal["start", "finish"],
            facts: t.Mapping[str, t.Any],
            error: t.Optional[BaseException],
        ) -> None:
            if previous is not None:
                try:
                    previous(phase, facts, error)
                except Exception:
                    logger.debug("sqlmesh-observability: chained observer failed", exc_info=True)
            try:
                get_collector().handle(invocation_id, phase, facts, error)  # type: ignore[arg-type]
            except Exception:
                logger.debug("sqlmesh-observability: collector failed", exc_info=True)

        return observe

    setattr(get_execution_observer, _MARKER, True)
    try:
        console.get_execution_observer = get_execution_observer
    except Exception:
        logger.debug("sqlmesh-observability: could not wrap console", exc_info=True)
        return False
    return True


def _ensure_current_console_wrapped() -> None:
    """Re-apply the wrap if another package replaced the console after ``install()``.

    Runs from the start hooks, which SQLMesh calls before it resolves the observer.
    """
    from sqlmesh.core.console import get_console

    wrap_console(get_console())


# SQLMesh declares BaseNotificationTarget frozen but GenericNotificationTarget not, so mypy rejects
# any subclass of it, frozen or not. At runtime the model is frozen through its base.
class ObservabilityTarget(GenericNotificationTarget):  # type: ignore[misc]
    """Supplies environment and plan id per invocation, and closes it. Never raises."""

    notify_on: t.FrozenSet[NotificationEvent] = _LIFECYCLE_EVENTS

    def send(self, notification_status: NotificationStatus, msg: str, **kwargs: t.Any) -> None:
        """No messages: this target only marks invocation boundaries."""

    def notify_apply_start(
        self, environment: str, plan_id: str, *args: t.Any, **kwargs: t.Any
    ) -> None:
        _start("plan", environment, plan_id)

    def notify_run_start(self, environment: str, *args: t.Any, **kwargs: t.Any) -> None:
        _start("run", environment, None)

    def notify_apply_end(
        self, environment: str, plan_id: str, *args: t.Any, **kwargs: t.Any
    ) -> None:
        _end("success")

    def notify_run_end(self, environment: str, *args: t.Any, **kwargs: t.Any) -> None:
        _end("success")

    def notify_apply_failure(
        self, environment: str, plan_id: str, exc: str, *args: t.Any, **kwargs: t.Any
    ) -> None:
        _end("failure")

    def notify_run_failure(self, exc: str, *args: t.Any, **kwargs: t.Any) -> None:
        _end("failure")


def _start(command: str, environment: t.Optional[str], plan_id: t.Optional[str]) -> None:
    try:
        _ensure_current_console_wrapped()
    except Exception:
        logger.debug("sqlmesh-observability: console re-check failed", exc_info=True)
    try:
        get_collector().arm(command, environment, plan_id)
    except Exception:
        logger.debug("sqlmesh-observability: could not record start", exc_info=True)


def _end(status: t.Literal["success", "failure"]) -> None:
    try:
        get_collector().end(status)
    except Exception:
        logger.debug("sqlmesh-observability: could not record end", exc_info=True)
