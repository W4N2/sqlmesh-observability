"""Tests: chart data, health strips, sample rows, metadata, and the offline page.

The page draws charts in the browser from the embedded payload, so these tests check the payload
and the HTML fallback with stdlib ``html.parser``. Drawing is checked in a browser against the
demo report.
"""

from __future__ import annotations

import re
import typing as t
from html.parser import HTMLParser
from pathlib import Path

import pytest
from conftest import Probe
from test_report import (  # noqa: F401  (collector is a fixture)
    FIVE_NULL_ROWS,
    ONE_ROW,
    _install_on_load,
    _plan,
    _write_model,
    collector,
)
from test_report_data import decode


class _Text(HTMLParser):
    """Visible text per element id, outside scripts and styles."""

    def __init__(self, document: str) -> None:
        super().__init__()
        self.sections: t.Dict[str, t.List[str]] = {}
        self._ids: t.List[t.Optional[str]] = []
        self._skip = 0
        self.feed(document)

    def handle_starttag(self, tag: str, attrs: t.List[t.Tuple[str, t.Optional[str]]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        self._ids.append(dict(attrs).get("id"))

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip -= 1
        if self._ids:
            self._ids.pop()

    def handle_data(self, data: str) -> None:
        if self._skip or not data.strip():
            return
        for elem_id in self._ids:
            if elem_id:
                self.sections.setdefault(elem_id, []).append(data.strip())


class Element(t.NamedTuple):
    tag: str
    attrs: t.Dict[str, str]
    title: str
    ancestors: t.Tuple[str, ...]  # ids and classes of enclosing elements


class _Elements(HTMLParser):
    """Every element with its attributes, its direct ``<title>`` text, and its ancestors."""

    VOID = {"meta", "link", "br", "img", "input", "hr"}

    def __init__(self, document: str) -> None:
        super().__init__()
        self.elements: t.List[Element] = []
        self._stack: t.List[t.Tuple[str, t.Dict[str, str], t.List[str]]] = []
        self._in_title = False
        self.feed(document)

    def handle_starttag(self, tag: str, attrs: t.List[t.Tuple[str, t.Optional[str]]]) -> None:
        attr_map = {k: v or "" for k, v in attrs}
        if tag == "title" and self._stack:
            self._in_title = True
            return
        if tag in self.VOID:
            self.elements.append(Element(tag, attr_map, "", self._ancestry()))
            return
        self._stack.append((tag, attr_map, []))

    def handle_startendtag(self, tag: str, attrs: t.List[t.Tuple[str, t.Optional[str]]]) -> None:
        self.elements.append(Element(tag, {k: v or "" for k, v in attrs}, "", self._ancestry()))

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self._in_title = False
            return
        if self._stack and self._stack[-1][0] == tag:
            name, attrs, title = self._stack.pop()
            self.elements.append(Element(name, attrs, "".join(title), self._ancestry()))

    def handle_data(self, data: str) -> None:
        if self._in_title and self._stack:
            self._stack[-1][2].append(data)

    def _ancestry(self) -> t.Tuple[str, ...]:
        found: t.List[str] = []
        for _, attrs, _ in self._stack:
            if attrs.get("id"):
                found.append("#" + attrs["id"])
            found.extend("." + c for c in attrs.get("class", "").split())
        return tuple(found)

    def find(self, tag: str, cls: t.Optional[str] = None) -> t.List[Element]:
        return [
            e
            for e in self.elements
            if e.tag == tag and (cls is None or cls in e.attrs.get("class", "").split())
        ]


def _spike_project(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context

    _install_on_load(probe, monitors={"volume": True})
    _write_model(
        project,
        "events.sql",
        "obs.events",
        "SELECT CAST(@start_ds AS DATE) AS ds, id FROM raw.src",
        "not_null(columns := (id))",
    )
    context = Context(paths=str(project))
    context.engine_adapter.execute("CREATE SCHEMA IF NOT EXISTS raw")
    context.engine_adapter.execute("CREATE TABLE raw.src AS SELECT range AS id FROM range(10)")
    context.load()
    _plan(context, execution_time="2024-01-09")
    context.engine_adapter.execute("INSERT INTO raw.src SELECT range FROM range(10, 500)")
    context.run(execution_time="2024-01-10")
    context.close()


@pytest.mark.usefixtures("collector")
def test_report_carries_anomaly_series_with_band_and_text(project: Path, probe: Probe) -> None:
    from sqlmesh_observability.payload import undelta
    from sqlmesh_observability.reader import load_report_data
    from sqlmesh_observability.report import render_report

    _spike_project(project, probe)
    data = load_report_data(project)
    (series,) = data.models[0].metrics
    assert series.threshold == 3.0

    document = render_report(data)
    payload = decode(document)
    (model,) = payload["models"]
    ((metric, column, threshold, dates, values, means, sds, flags),) = model["m"]
    assert payload["metrics"][metric] == "row_count" and column == "" and threshold == 3.0
    assert len(values) == 9
    assert [payload["dates"][d] for d in undelta(dates)][-1] == "2024-01-09"
    assert flags[-1] & 1 and not any(f & 1 for f in flags[:-1])
    assert means[-1] is not None and sds[-1] is not None  # the expected band
    # Not color alone: the anomaly is listed as text with its value and expected range.
    text = " ".join(_Text(document).sections.get("overview-anomalies", []))
    assert "row_count" in text and "2024-01-09" in text and "expected" in text


@pytest.mark.usefixtures("collector")
def test_report_renders_interval_health_strip_and_duration_series(
    project: Path, probe: Probe
) -> None:
    from sqlmesh.core.context import Context
    from sqlmesh.utils.errors import PlanError

    from sqlmesh_observability.reader import load_report_data
    from sqlmesh_observability.report import render_report

    _install_on_load(probe)
    _write_model(
        project,
        "events.sql",
        "obs.events",
        "SELECT ds, id FROM raw.src WHERE ds BETWEEN @start_ds AND @end_ds",
        "not_null(columns := (id))",
    )
    context = Context(paths=str(project))
    context.engine_adapter.execute("CREATE SCHEMA IF NOT EXISTS raw")
    context.engine_adapter.execute(
        "CREATE TABLE raw.src AS SELECT * FROM (VALUES (DATE '2024-01-01', 1), "
        "(DATE '2024-01-02', NULL)) AS v(ds, id)"
    )
    context.load()
    with pytest.raises(PlanError):
        _plan(context)
    context.close()

    data = load_report_data(project)
    (model,) = data.models
    assert {(h.interval_start[:10], h.status) for h in model.health} == {
        ("2024-01-01", "pass"),
        ("2024-01-02", "fail"),
    }
    assert model.runs and all(r.duration_ms is not None for r in model.runs)

    from sqlmesh_observability.payload import undelta

    (stored,) = decode(render_report(data))["models"]
    dates, codes = stored["h"]
    days = undelta(dates)
    payload = decode(render_report(data))
    assert {(payload["dates"][d], c) for d, c in zip(days, codes)} == {
        ("2024-01-01", 0),
        ("2024-01-02", 2),
    }
    assert len(stored["r"][1]) == 2 and all(ms is not None for ms in stored["r"][1])


@pytest.mark.usefixtures("collector")
def test_report_renders_structured_sample_table(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context
    from sqlmesh.utils.errors import PlanError

    from sqlmesh_observability.reader import load_report_data
    from sqlmesh_observability.report import render_report

    _install_on_load(probe, sample_rows=2)
    _write_model(project, "events.sql", "obs.events", FIVE_NULL_ROWS, "not_null(columns := (id))")
    context = Context(paths=str(project))
    with pytest.raises(PlanError):
        _plan(context)
    context.close()

    payload = decode(render_report(load_report_data(project)))
    assert payload["incidents"]
    for incident in payload["incidents"]:
        assert incident["sql"]
        assert len(incident["samples"]) == 2
        assert all(list(row) == ["ds", "id"] for row in incident["samples"])


@pytest.mark.usefixtures("collector")
def test_report_hydrates_unpromoted_snapshot_metadata(project: Path, probe: Probe) -> None:
    from sqlmesh.core.context import Context
    from sqlmesh.utils.errors import PlanError

    from sqlmesh_observability.reader import load_report_data

    _install_on_load(probe)
    _write_model(project, "events.sql", "obs.events", ONE_ROW, "not_null(columns := (id))")
    context = Context(paths=str(project))
    _plan(context)
    _write_model(
        project,
        "broken.sql",
        "obs.broken",
        FIVE_NULL_ROWS,
        "not_null(columns := (id))",
        owner="payments-team",
    )
    context.load()
    with pytest.raises(PlanError):
        _plan(context)
    context.close()

    data = load_report_data(project)
    broken = next(m for m in data.models if m.model_name.endswith('"broken"'))
    assert broken.owner == "payments-team"
    assert {name for name, _ in broken.columns} == {"ds", "id"}


@pytest.mark.usefixtures("collector")
def test_report_loads_no_external_assets_and_has_a_fallback(project: Path, probe: Probe) -> None:
    from sqlmesh_observability.reader import load_report_data
    from sqlmesh_observability.report import render_report

    _spike_project(project, probe)
    html = render_report(load_report_data(project))
    assert not re.search(r"""(src|href)\s*=\s*["']?\s*(https?:)?//""", html, re.I)
    assert "@import" not in html

    page = _Elements(html)
    # Without JavaScript the fallback summary is shown; nothing in it starts hidden.
    fallback = [e for e in page.elements if "#fallback" in e.ancestors]
    assert fallback and not [e for e in fallback if "hidden" in e.attrs]
    assert page.find("div") and [e for e in page.elements if e.attrs.get("id") == "app"]
    assert decode(html)["models"]
