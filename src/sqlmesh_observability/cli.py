"""``sqlmesh-observe`` command line entry point.

Commands: ``init-audits``, ``report``, ``serve``, and ``monitor``.
"""

from __future__ import annotations

import argparse
import sys
import typing as t


def _init_audits(args: argparse.Namespace) -> int:
    from sqlmesh_observability.audits import copy_audits

    try:
        written = copy_audits(args.path, force=args.force)
    except FileExistsError as error:
        print(f"sqlmesh-observe: {error}", file=sys.stderr)
        return 1
    for path in written:
        print(f"wrote {path}")
    if not written:
        print("audits are up to date")
    return 0


def _report(args: argparse.Namespace) -> int:
    from sqlmesh_observability.reader import MissingAuditTableError, load_report_data
    from sqlmesh_observability.report import write_report

    try:
        data = load_report_data(
            path=args.path,
            gateway=args.gateway,
            schema=args.schema,
            environment=args.environment,
        )
    except MissingAuditTableError as error:
        print(f"sqlmesh-observe: {error}", file=sys.stderr)
        return 1

    if not data.state_available:
        print(
            f"sqlmesh-observe: SQLMesh state for environment '{data.environment}' could not be "
            "read; the report shows only the latest invocation that targeted it.",
            file=sys.stderr,
        )
    written = write_report(data, args.output, history=args.history)
    print(f"wrote {written}")
    return 0


def _serve(args: argparse.Namespace) -> int:
    from sqlmesh_observability.app import SERVE_EXTRA_HINT, create_app

    try:
        import uvicorn

        app = create_app(
            path=args.path,
            gateway=args.gateway,
            schema=args.schema,
            environment=args.environment,
            history=args.history,
        )
    except ImportError:
        print(f"sqlmesh-observe: {SERVE_EXTRA_HINT}", file=sys.stderr)
        return 1
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _monitor(args: argparse.Namespace) -> int:
    from sqlmesh_observability.alerts import run_monitor
    from sqlmesh_observability.reader import MissingAuditTableError

    try:
        return run_monitor(
            path=args.path,
            gateway=args.gateway,
            schema=args.schema,
            environment=args.environment,
        )
    except MissingAuditTableError as error:
        print(f"sqlmesh-observe: {error}", file=sys.stderr)
        return 1


def _add_source_options(command: argparse.ArgumentParser) -> None:
    """The options every read-side command shares: where the log is and which environment."""
    command.add_argument(
        "--path", default=".", help="Directory containing config.py (model files are not required)."
    )
    command.add_argument(
        "--gateway",
        default=None,
        help="Gateway holding observability tables (defaults to install() or default_gateway).",
    )
    command.add_argument(
        "--schema",
        default=None,
        help="Observability schema (defaults to install() schema or 'observability').",
    )
    command.add_argument(
        "--environment",
        default="prod",
        help="Target environment to scope to (default: prod).",
    )


def _add_history_option(command: argparse.ArgumentParser) -> None:
    from sqlmesh_observability.payload import DEFAULT_HISTORY

    command.add_argument(
        "--history",
        type=int,
        default=DEFAULT_HISTORY,
        help=f"Intervals of history to keep per model and metric (default: {DEFAULT_HISTORY}).",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sqlmesh-observe")
    commands = parser.add_subparsers(dest="command", required=True)

    init_audits = commands.add_parser(
        "init-audits", help="Copy the package audits into the project's audits/ directory."
    )
    init_audits.add_argument("--path", default=".", help="SQLMesh project directory.")
    init_audits.add_argument(
        "--force", action="store_true", help="Overwrite audit files that differ."
    )
    init_audits.set_defaults(handler=_init_audits)

    report = commands.add_parser(
        "report", help="Generate a self-contained static HTML observability report."
    )
    _add_source_options(report)
    _add_history_option(report)
    report.add_argument(
        "-o",
        "--output",
        default="report.html",
        help="Output HTML file path (default: report.html).",
    )
    report.set_defaults(handler=_report)

    serve = commands.add_parser(
        "serve", help="Serve the report as a read-only web app (needs the [serve] extra)."
    )
    _add_source_options(serve)
    _add_history_option(serve)
    serve.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1).")
    serve.add_argument("--port", type=int, default=8000, help="Port (default: 8000).")
    serve.set_defaults(handler=_serve)

    monitor = commands.add_parser(
        "monitor",
        help="Send one grouped message per owner for new prod audit failures and anomalies.",
    )
    _add_source_options(monitor)
    monitor.set_defaults(handler=_monitor)
    return parser


def main(argv: t.Optional[t.Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    return int(args.handler(args))


def entrypoint() -> None:
    sys.exit(main())
