"""``sqlmesh-observe serve``: a read-only ASGI app over the observability report.

Every route reads the warehouse log and SQLMesh state through ``load_report_data`` and renders
the same document as ``sqlmesh-observe report``, opened on the requested model or incident. Only
GET (and HEAD) routes exist. Nothing here runs a model, applies a plan, loads a ``Context``, or
writes.

Starlette is an optional dependency (``sqlmesh-observability[serve]``), imported inside
:func:`create_app`.
"""

from __future__ import annotations

import html
import typing as t
from pathlib import Path

if t.TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response

    from sqlmesh_observability.reader import AuditRecord, ModelReport, ReportData

SERVE_EXTRA_HINT = (
    "sqlmesh-observe serve needs Starlette and uvicorn: pip install 'sqlmesh-observability[serve]'"
)


def find_model(data: "ReportData", name: str) -> t.Optional["ModelReport"]:
    """The model named ``name``: its full name, the same without quotes, or a unique suffix.

    ``"db"."obs"."events"``, ``db.obs.events`` and ``obs.events`` all find the same model, unless
    another model shares that suffix.
    """
    plain = name.replace('"', "")
    for model in data.models:
        if model.model_name == name or model.model_name.replace('"', "") == plain:
            return model
    matches = [
        m for m in data.models if m.model_name.replace('"', "").endswith("." + plain.lstrip("."))
    ]
    return matches[0] if len(matches) == 1 else None


def find_incident(
    data: "ReportData", invocation_id: str, audit_name: str
) -> t.Optional["AuditRecord"]:
    """The newest in-scope failed or errored audit with this invocation and audit name."""
    for incident in data.incidents:  # newest first
        if incident.invocation_id == invocation_id and incident.audit_name == audit_name:
            return incident
    return None


def _message_page(status: int, title: str, detail: str) -> str:
    return (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        f"<title>{html.escape(title)} · sqlmesh-observe</title></head>"
        f"<body><h1>{status} {html.escape(title)}</h1><p>{html.escape(detail)}</p>"
        '<p><a href="/">Back to the dashboard</a></p></body></html>\n'
    )


def create_app(
    path: t.Union[str, Path] = ".",
    gateway: t.Optional[str] = None,
    schema: t.Optional[str] = None,
    environment: str = "prod",
) -> "Starlette":
    """Build the read-only app for the project whose ``config.py`` is in ``path``."""
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse
    from starlette.routing import Route

    from sqlmesh_observability.reader import MissingAuditTableError, load_report_data
    from sqlmesh_observability.report import _incident_anchor, _model_anchor, render_report

    def page(request: "Request", pick: t.Callable[["ReportData"], t.Optional[str]]) -> "Response":
        # Runs on the event loop's thread, not Starlette's threadpool: hydrating snapshots from
        # state can start SQLMesh's fork-based process pool, and forking from a worker thread
        # crashes on macOS. Requests are therefore served one at a time.
        try:
            data = load_report_data(
                path=path, gateway=gateway, schema=schema, environment=environment
            )
        except MissingAuditTableError as error:
            return HTMLResponse(_message_page(503, "No observability data", str(error)), 503)
        start = pick(data)
        if start is None:
            return HTMLResponse(
                _message_page(404, "Not found", f"Nothing in scope for {request.url.path}."), 404
            )
        return HTMLResponse(render_report(data, start=start or None))

    async def overview(request: "Request") -> "Response":
        return page(request, lambda data: "")

    async def model(request: "Request") -> "Response":
        name = request.path_params["name"]

        def pick(data: "ReportData") -> t.Optional[str]:
            found = find_model(data, name)
            return _model_anchor(found.model_name) if found else None

        return page(request, pick)

    async def incident(request: "Request") -> "Response":
        invocation_id = request.path_params["invocation_id"]
        audit_name = request.path_params["audit_name"]

        def pick(data: "ReportData") -> t.Optional[str]:
            found = find_incident(data, invocation_id, audit_name)
            return _incident_anchor(found) if found else None

        return page(request, pick)

    return Starlette(
        routes=[
            Route("/", overview, methods=["GET"]),
            Route("/models/{name:path}", model, methods=["GET"]),
            Route("/incidents/{invocation_id}/{audit_name}", incident, methods=["GET"]),
        ]
    )
