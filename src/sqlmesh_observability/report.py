"""The self-contained HTML report.

One offline file: a JSON payload (``payload.py``) that the inline script (``web/report.js``) draws
in the browser, plus a short HTML fallback written here for readers without JavaScript and for
HTML parsers: the headline status, the counts, and every open issue.
"""

from __future__ import annotations

import html
import typing as t
from importlib import resources
from pathlib import Path
from urllib.parse import quote

from sqlmesh_observability.payload import DEFAULT_HISTORY, build_payload, encode
from sqlmesh_observability.textfmt import number

if t.TYPE_CHECKING:
    from sqlmesh_observability.reader import ReportData


def _esc(value: t.Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _asset(name: str) -> str:
    web = resources.files("sqlmesh_observability").joinpath("web")
    return web.joinpath(name).read_text(encoding="utf-8")


def model_route(model_name: str) -> str:
    """The page route that opens a model, as used in ``data-start`` and URL fragments."""
    return "model?name=" + quote(model_name, safe="")


def incident_route(index: int) -> str:
    """The page route that opens the incident at ``index`` in the payload's incident list."""
    return f"incidents?i={index}"


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


_GROUPS = (
    (
        "overview-failures",
        "Blocking failures and errors",
        ("blocking", "error"),
        "No blocking failures",
    ),
    ("overview-anomalies", "Anomalies at the latest interval", ("anomaly",), "No anomalies"),
    ("overview-warnings", "Non-blocking failures", ("warning",), "No non-blocking failures"),
)


def _fallback(payload: t.Mapping[str, t.Any]) -> str:
    models = payload["models"]
    audits = payload["audits"]
    summary = payload["summary"]
    failing, warning = summary["models"]["fail"], summary["models"]["warn"]
    if failing:
        status = _plural(failing, "failing model")
    elif warning:
        status = f"{_plural(warning, 'model')} with warnings"
    else:
        status = "Healthy"

    lineage = payload["lineage"]
    names = [m["n"] for m in models] + lineage["extra"]
    children: t.Dict[int, t.List[int]] = {}
    for child, parents in enumerate(lineage["parents"]):
        for parent in parents:
            children.setdefault(parent, []).append(child)

    def downstream(model: int) -> str:
        direct = sorted(names[c].replace('"', "") for c in children.get(model, []))
        if not direct:
            return "-"
        shown = ", ".join(direct[:10])
        return shown + (f", and {len(direct) - 10:,} more" if len(direct) > 10 else "")

    def row(issue: t.Mapping[str, t.Any]) -> str:
        model = models[issue["model"]]["n"].replace('"', "") if issue["model"] >= 0 else "-"
        if issue["kind"] == "anomaly":
            signal = issue["signal"] + (f" ({issue['column']})" if issue.get("column") else "")
            value = (
                f"{number(issue['value'])} (expected {number(issue['lo'])} – {number(issue['hi'])})"
            )
        else:
            name, args = audits[issue["use"]]
            signal = f"{name} {args}".strip()
            value = "-" if issue["value"] is None else _plural(issue["value"], "row")
        return (
            f"<tr><td><code>{_esc(model)}</code></td><td><code>{_esc(signal)}</code></td>"
            f"<td>{_esc(issue['interval'] or '-')}</td><td>{_esc(value)}</td>"
            f"<td>{_esc(issue['kind'])}</td>"
            f"<td>{_esc(downstream(issue['model']) if issue['model'] >= 0 else '-')}</td></tr>"
        )

    groups = []
    for group_id, title, kinds, empty in _GROUPS:
        rows = [row(i) for i in payload["issues"] if i["kind"] in kinds]
        body = "".join(rows) or f'<tr><td colspan="6" class="note">{_esc(empty)}</td></tr>'
        groups.append(
            f'<tbody id="{group_id}"><tr class="group-head"><td colspan="6">{_esc(title)} · '
            f"{len(rows)}</td></tr>{body}</tbody>"
        )

    state = ""
    if not payload["state_available"]:
        state = (
            '<p class="state-warning" id="state-warning">SQLMesh state for '
            f"{_esc(payload['environment'])} could not be read. Showing only the latest "
            "invocation that targeted it.</p>"
        )
    a = summary["audits"]
    return (
        '<div id="fallback" class="fallback"><section id="overview">'
        f"<h1>{_esc(payload['environment'])}: {_esc(status)}</h1>"
        f'<p class="meta">{_esc(payload["schema"])} · {_esc(payload["gateway"])} · generated '
        f"{_esc(payload['generated_at'])}</p>{state}"
        '<ul class="counts">'
        f"<li><strong>{failing:,}</strong>failing models</li>"
        f"<li><strong>{warning:,}</strong>models with warnings</li>"
        f"<li><strong>{a['pass']:,}</strong>passing audits</li>"
        f"<li><strong>{len(payload['issues']):,}</strong>open issues</li>"
        f"<li><strong>{len(models):,}</strong>models</li></ul>"
        "<table><thead><tr><th>Model</th><th>Signal</th><th>Interval</th><th>Value</th>"
        f"<th>Severity</th><th>Downstream</th></tr></thead>{''.join(groups)}</table>"
        '<p class="note">Turn on JavaScript to see model pages, test results, runs, and charts.'
        "</p></section></div>"
    )


def render_report(
    data: "ReportData", start: t.Optional[str] = None, history: int = DEFAULT_HISTORY
) -> str:
    """Render ``data`` as one self-contained HTML5 document.

    ``start`` is the route to open when the URL has no fragment (``serve`` sets it for a model or
    an incident; see :func:`model_route` and :func:`incident_route`).
    """
    payload = build_payload(data, history=history)
    return render_payload(payload, start=start)


def render_payload(payload: t.Mapping[str, t.Any], start: t.Optional[str] = None) -> str:
    start_attr = f' data-start="{_esc(start)}"' if start else ""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '  <meta name="color-scheme" content="dark light">\n'
        f"  <title>sqlmesh-observe · {_esc(payload['environment'])}</title>\n"
        f"  <style>{_asset('report.css')}</style>\n"
        "</head>\n"
        f"<body{start_attr}>\n"
        f"{_fallback(payload)}\n"
        '<div id="app"></div>\n'
        f'<script type="application/json" id="report-data">{encode(payload)}</script>\n'
        f'<script id="report-app">{_asset("report.js")}</script>\n'
        "</body>\n"
        "</html>\n"
    )


def write_report(
    data: "ReportData", output: t.Union[str, Path], history: int = DEFAULT_HISTORY
) -> Path:
    """Render ``data`` and write it to ``output``, creating parent directories if needed."""
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_report(data, history=history), encoding="utf-8")
    return destination
