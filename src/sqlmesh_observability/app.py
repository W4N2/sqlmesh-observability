"""``sqlmesh-observe serve``: a read-only ASGI app over the observability report.

Every route reads the warehouse log and SQLMesh state through ``load_report_data`` and returns
the same page as ``sqlmesh-observe report``, opened on the requested model or incident, or the
page's JSON payload (``/data.json``). Only GET (and HEAD) routes exist. Nothing here runs a
model, applies a plan, loads a ``Context``, or writes.

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
    history: t.Optional[int] = None,
) -> "Starlette":
    """Build the read-only app for the project whose ``config.py`` is in ``path``."""
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, Response
    from starlette.routing import Route

    from sqlmesh_observability.payload import DEFAULT_HISTORY, build_payload, encode
    from sqlmesh_observability.reader import MissingAuditTableError, load_report_data
    from sqlmesh_observability.report import incident_route, model_route, render_payload

    keep = DEFAULT_HISTORY if history is None else history

    def load() -> t.Union["Response", t.Tuple["ReportData", t.Dict[str, t.Any]]]:
        # Runs on the event loop's thread, not Starlette's threadpool: hydrating snapshots from
        # state can start SQLMesh's fork-based process pool, and forking from a worker thread
        # crashes on macOS. Requests are therefore served one at a time.
        try:
            data = load_report_data(
                path=path, gateway=gateway, schema=schema, environment=environment
            )
        except MissingAuditTableError as error:
            return HTMLResponse(_message_page(503, "No observability data", str(error)), 503)
        return data, build_payload(data, history=keep)

    def page(
        request: "Request",
        pick: t.Callable[["ReportData", t.Dict[str, t.Any]], t.Optional[str]],
    ) -> "Response":
        loaded = load()
        if not isinstance(loaded, tuple):
            return loaded
        data, payload = loaded
        start = pick(data, payload)
        if start is None:
            return HTMLResponse(
                _message_page(404, "Not found", f"Nothing in scope for {request.url.path}."), 404
            )
        return HTMLResponse(render_payload(payload, start=start or None))

    async def overview(request: "Request") -> "Response":
        return page(request, lambda data, payload: "")

    async def model(request: "Request") -> "Response":
        name = request.path_params["name"]

        def pick(data: "ReportData", payload: t.Dict[str, t.Any]) -> t.Optional[str]:
            found = find_model(data, name)
            return model_route(found.model_name) if found else None

        return page(request, pick)

    async def incident(request: "Request") -> "Response":
        invocation_id = request.path_params["invocation_id"]
        audit_name = request.path_params["audit_name"]

        def pick(data: "ReportData", payload: t.Dict[str, t.Any]) -> t.Optional[str]:
            if find_incident(data, invocation_id, audit_name) is None:
                return None
            matches = [
                (stored.get("started_at") or "", index)
                for index, stored in enumerate(payload["incidents"])
                if stored["invocation"] == invocation_id
                and payload["audits"][stored["use"]][0] == audit_name
            ]
            # Outside the retained history window: open the incident list instead.
            return incident_route(max(matches)[1]) if matches else "incidents"

        return page(request, pick)

    async def data_json(request: "Request") -> "Response":
        loaded = load()
        if not isinstance(loaded, tuple):
            return loaded
        return Response(encode(loaded[1]), media_type="application/json")

    return Starlette(
        routes=[
            Route("/", overview, methods=["GET"]),
            Route("/data.json", data_json, methods=["GET"]),
            Route("/models/{name:path}", model, methods=["GET"]),
            Route("/incidents/{invocation_id}/{audit_name}", incident, methods=["GET"]),
        ]
    )
