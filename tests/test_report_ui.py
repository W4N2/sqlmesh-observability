"""Tests: report charts, health strips, sample tables, metadata.

HTML is checked with stdlib ``html.parser``;
no browser and no full-HTML snapshots.
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
def test_report_renders_svg_anomaly_chart_with_band_and_markers(
    project: Path, probe: Probe
) -> None:
    from sqlmesh_observability.reader import load_report_data
    from sqlmesh_observability.report import render_report

    _spike_project(project, probe)
    data = load_report_data(project)
    (series,) = data.models[0].metrics
    assert series.threshold == 3.0

    page = _Elements(render_report(data))
    charts = [e for e in page.find("svg", "chart")]
    assert len(charts) == 1
    assert "row_count" in charts[0].attrs.get("aria-label", "") or "Row count" in charts[0].title

    def in_chart(element: Element) -> bool:
        return ".chart" in element.ancestors

    assert [e for e in page.find("path", "band") if in_chart(e)]
    assert [e for e in page.find("path", "line") if in_chart(e)]
    points = [e for e in page.find("circle", "point") if in_chart(e)]
    assert len(points) == 9
    (anomaly,) = [e for e in page.find("circle", "anomaly") if in_chart(e)]
    assert anomaly.title.startswith("Anomaly") and "2024-01-09" in anomaly.title
    # Not color alone: the anomaly is also listed as text next to the chart.
    notes = [e for e in page.find("li", "anomaly-note")]
    assert notes


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

    page = _Elements(render_report(data))
    cells = page.find("rect", "cell")
    assert {c.attrs.get("data-status") for c in cells} == {"pass", "fail"}
    assert any("2024-01-02" in c.title and "fail" in c.title for c in cells)
    assert page.find("path", "duration")


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

    page = _Elements(render_report(load_report_data(project)))
    tables = [e for e in page.find("table", "samples")]
    assert tables
    headers = [e for e in page.find("th") if ".samples" in e.ancestors]
    rows = [e for e in page.find("tr") if ".samples" in e.ancestors]
    header_text = render_report(load_report_data(project))
    assert re.search(r'<table class="samples">.*?<th>ds</th><th>id</th>', header_text, re.S)
    assert headers
    assert page.find("details", "sql")  # rendered SQL is collapsible
    # Two sampled rows per incident, plus the header row, per sample table.
    assert len(rows) == 3 * len(tables)


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
def test_report_loads_no_external_assets_and_filters_work_without_js(
    project: Path, probe: Probe
) -> None:
    from sqlmesh_observability.reader import load_report_data
    from sqlmesh_observability.report import render_report

    _spike_project(project, probe)
    html = render_report(load_report_data(project))
    assert not re.search(r"""(src|href)\s*=\s*["']?\s*(https?:)?//""", html, re.I)
    assert "@import" not in html

    page = _Elements(html)
    # Everything is visible without JavaScript: nothing starts hidden.
    assert not [e for e in page.elements if "hidden" in e.attrs]
    cards = [e for e in page.find("article") if "data-model" in e.attrs]
    assert cards and all("data-status" in e.attrs for e in cards)
    assert page.find("nav", "sidebar")
