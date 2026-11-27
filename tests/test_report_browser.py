"""Browser tests: the report renderer (``web/report.js``) in headless Chromium.

The Python tests pin the payload and the HTML fallback; these check what the browser draws from
them, against the 1,500-model synthetic fixture and the small DuckDB demo project.

Needs ``playwright`` (in the ``dev`` extra, Python 3.10+) and its Chromium::

    python -m playwright install chromium

Without them the tests skip. With ``REQUIRE_BROWSER_TESTS=1`` (set in CI) they fail instead.
"""

from __future__ import annotations

import importlib.util
import os
import re
import typing as t
from pathlib import Path

import pytest

REQUIRED = os.environ.get("REQUIRE_BROWSER_TESTS") == "1"


def _unavailable(reason: str) -> None:
    if REQUIRED:
        pytest.fail(f"browser tests are required here, but {reason}")
    pytest.skip(reason, allow_module_level=True)


if importlib.util.find_spec("playwright") is None:
    _unavailable("playwright is not installed (pip install -e '.[dev]' on Python 3.10+)")

from playwright.sync_api import Browser, Page, expect  # noqa: E402

VIEWS = ("overview", "models", "lineage", "tests", "incidents", "runs", "schema")


@pytest.fixture(scope="session")
def browser(playwright: t.Any) -> t.Iterator["Browser"]:
    try:
        launched = playwright.chromium.launch()
    except Exception as error:  # Chromium not downloaded
        _unavailable(
            f"Chromium is not installed: run `python -m playwright install chromium` ({error})"
        )
    yield launched
    launched.close()


@pytest.fixture(scope="module")
def reports(tmp_path_factory: pytest.TempPathFactory) -> t.Dict[str, str]:
    """``file://`` URLs for the 1,500-model report, the small DuckDB demo, and a report whose
    payload has an unknown schema version."""
    from scale_fixture import build_scale_data

    from sqlmesh_observability.report import write_report

    root = tmp_path_factory.mktemp("reports")
    scale = write_report(build_scale_data(n=1500, days=90), root / "scale.html")

    spec = importlib.util.spec_from_file_location(
        "build_demo", Path(__file__).resolve().parent.parent / "scripts" / "build_demo.py"
    )
    assert spec and spec.loader
    build_demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_demo)
    previous = Path.cwd()
    work = tmp_path_factory.mktemp("demo")
    os.chdir(work)  # reading SQLMesh state writes a cache into the working directory
    try:
        demo = build_demo.build(work / "project", root / "demo.html")
    finally:
        os.chdir(previous)

    future = root / "future.html"
    future.write_text(
        demo.read_text(encoding="utf-8").replace('"schema_version":2,', '"schema_version":99,', 1),
        encoding="utf-8",
    )
    return {
        name: path.as_uri() for name, path in (("scale", scale), ("demo", demo), ("future", future))
    }


@pytest.fixture
def page(browser: "Browser") -> t.Iterator["Page"]:
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    opened = context.new_page()
    errors: t.List[str] = []
    opened.on("pageerror", lambda error: errors.append(str(error)))
    opened.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    yield opened
    context.close()
    assert errors == [], f"console errors: {errors}"


def _open(page: "Page", url: str, route: str = "overview") -> None:
    page.goto(f"{url}#{route}")
    expect(page.locator("html")).to_have_attribute("data-ready", "")


@pytest.mark.parametrize("report", ["scale", "demo"])
def test_every_view_renders(page: "Page", reports: t.Dict[str, str], report: str) -> None:
    _open(page, reports[report])
    expect(page.locator("#fallback")).to_be_hidden()
    for view in VIEWS:
        page.evaluate(f"location.hash = '{view}'")
        expect(page.locator(f'.tabs a[data-view="{view}"]')).to_have_attribute(
            "aria-current", "page"
        )
        expect(page.locator(".page .panel").first).to_be_visible()
    first = page.evaluate("window.__report.models[0].name")
    page.evaluate("name => { location.hash = 'model?name=' + encodeURIComponent(name) }", first)
    expect(page.locator(".model-head h1")).to_be_visible()


def test_unknown_schema_version_keeps_the_fallback(page: "Page", reports: t.Dict[str, str]) -> None:
    page.goto(reports["future"])
    expect(page.locator("#fallback")).to_be_visible()
    expect(page.locator("#fallback h1")).to_contain_text("failing model")
    assert page.evaluate("document.documentElement.hasAttribute('data-ready')") is False


def test_url_filters_apply_on_load(page: "Page", reports: t.Dict[str, str]) -> None:
    _open(page, reports["scale"], "models?status=fail")
    expect(page.locator('select[name="status"]')).to_have_value("fail")
    expect(page.locator(".shown")).to_have_text("6 of 1,500")
    pills = page.locator(".list-row .pill").all_text_contents()
    assert pills and set(pills) == {"failing"}


def test_tree_counts_expansion_and_filter(page: "Page", reports: t.Dict[str, str]) -> None:
    _open(page, reports["scale"], "models")
    mart00 = page.locator('.tree-group[data-key="schema:mart_00"]')
    expect(mart00).to_have_attribute("data-open", "")
    assert mart00.locator(".badge").all_text_contents() == ["62", "32", "6"]
    assert page.locator('.tree-group[data-key="schema:mart_05"]').get_attribute("data-open") is None

    page.locator(".tree input").fill("model_0042")
    expect(page.locator(".tree-leaf")).to_have_count(1)
    expect(page.locator(".tree-leaf")).to_have_text("model_0042")


def test_long_lists_are_windowed_and_sortable(page: "Page", reports: t.Dict[str, str]) -> None:
    _open(page, reports["scale"], "tests?scope=all")
    expect(page.locator(".shown")).to_have_text("45,020 shown")
    page.locator(".list").evaluate("el => { el.scrollTop = 600000 }")
    page.wait_for_function("document.querySelectorAll('.list-row').length > 0")
    assert page.locator(".list-row").count() < 500

    page.get_by_role("button", name=re.compile("^Violations")).click()
    first = page.locator(".list-row").first.locator("[role=cell]").nth(4)
    expect(first).to_have_text("120")


def test_anomaly_row_expands_to_its_chart(page: "Page", reports: t.Dict[str, str]) -> None:
    _open(page, reports["demo"], "tests?q=volume_anomaly&status=warn")
    page.locator(".list-row").first.click()
    detail = page.locator(".list-detail")
    marker = detail.locator("circle.anomaly")
    expect(marker).to_have_count(1)
    assert (marker.get_attribute("aria-label") or "").startswith("Anomaly on 2024-01-16")
    expect(detail.locator("pre.sql")).to_be_hidden()  # the audit query is folded away

    points = page.evaluate(
        "window.__report.models.find(m => m.leaf === 'orders')"
        ".series.find(s => s.metric === 'row_count').values.length"
    )
    detail.get_by_role("button", name="Table").click()
    expect(detail.locator(".chart-table tbody tr")).to_have_count(points)


def test_theme_choice_survives_a_reload(page: "Page", reports: t.Dict[str, str]) -> None:
    page.emulate_media(color_scheme="light")
    _open(page, reports["demo"])
    page.locator("button.theme").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-ready", "")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")


@pytest.mark.parametrize(
    "route",
    [*VIEWS, "model?name=demo.mart_00.model_0020", "lineage", "lineage?view=impact"],
)
def test_views_fit_a_phone(page: "Page", reports: t.Dict[str, str], route: str) -> None:
    page.set_viewport_size({"width": 360, "height": 800})
    _open(page, reports["scale"], route)
    expect(page.locator(".page .panel").first).to_be_visible()
    width = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
    assert width[0] <= width[1], f"{route}: page is {width[0]}px wide in a {width[1]}px viewport"


def _lineage(page: "Page", reports: t.Dict[str, str], **params: str) -> None:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    _open(page, reports["scale"], "lineage" + (f"?{query}" if query else ""))
    expect(page.locator(".lineage svg")).to_be_visible()


def _nodes(page: "Page") -> t.List[str]:
    return page.locator(".lineage .node.model").evaluate_all(
        "nodes => nodes.map(n => n.dataset.name)"
    )


def test_lineage_focuses_one_model_with_depth_and_direction(
    page: "Page", reports: t.Dict[str, str]
) -> None:
    _lineage(page, reports, model="demo.mart_00.model_0000")
    assert len(_nodes(page)) <= 150
    focus = page.locator(".lineage .node.focus")
    expect(focus).to_have_count(1)
    assert focus.get_attribute("data-name") == "demo.mart_00.model_0000"
    card = focus.locator("text").all_text_contents()
    assert "mart_00 · full · team-00" in card  # schema, kind, owner
    assert "not_null (payment_id)" in card  # the failing audit by name
    assert "501 downstream" in card
    names = _nodes(page)
    assert "demo.raw.source_00" in names  # its external parent
    assert "demo.mart_03.model_0300" in names  # its child

    _lineage(page, reports, model="demo.mart_00.model_0000", depth="1", dir="down")
    assert sorted(_nodes(page)) == ["demo.mart_00.model_0000", "demo.mart_03.model_0300"]

    page.locator('.lineage .node[data-name="demo.mart_03.model_0300"]').click()
    expect(page.locator(".lineage .node.focus")).to_have_attribute(
        "data-name", "demo.mart_03.model_0300"
    )
    assert "model=demo.mart_03.model_0300" in page.url.replace("%2E", ".")


def test_lineage_opens_on_the_widest_impact_issue(page: "Page", reports: t.Dict[str, str]) -> None:
    _lineage(page, reports)
    widest = page.evaluate(
        "(() => { const d = window.__report.data;"
        " const top = d.issues.slice().sort((a, b) => b.impact - a.impact)[0];"
        " return d.models[top.model].n; })()"
    )
    expect(page.locator(".lineage .node.focus")).to_have_attribute("data-name", widest)


def test_lineage_bounds_a_wide_fan_out(page: "Page", reports: t.Dict[str, str]) -> None:
    _lineage(page, reports, model="demo.mart_03.model_0300", depth="1", dir="down")
    assert len(_nodes(page)) <= 1 + 20  # the focus and at most 20 children
    more = page.locator(".lineage .node.more")
    expect(more).to_have_count(1)
    hidden = int(more.get_attribute("data-count") or 0)
    assert len(_nodes(page)) - 1 + hidden == 400  # every child is drawn or counted
    more.click()
    expect(page.locator(".lineage-more li")).to_have_count(hidden)


def test_lineage_issues_only_collapses_healthy_steps(
    page: "Page", reports: t.Dict[str, str]
) -> None:
    _lineage(page, reports, model="demo.mart_10.model_1000", depth="all", dir="up", issues="1")
    names = _nodes(page)
    assert "demo.mart_00.model_0000" in names  # failing, two levels up
    assert "demo.mart_03.model_0300" not in names  # healthy, between them
    step = page.locator(".lineage .node.step")
    expect(step.first).to_be_visible()
    assert "healthy" in (step.first.text_content() or "")
    assert "demo.raw.source_00" not in names  # healthy, not between the focus and an issue


def test_impact_view_lists_issues_by_downstream_reach(
    page: "Page", reports: t.Dict[str, str]
) -> None:
    _open(page, reports["scale"], "lineage?view=impact")
    rows = page.locator(".list-row")
    expect(rows.first).to_be_visible()
    reach = [
        int(r.replace(",", ""))
        for r in rows.evaluate_all("rs => rs.map(r => r.children[3].textContent)")
    ]
    assert reach == sorted(reach, reverse=True) and reach[0] > 0
    rows.first.click()
    detail = page.locator(".list-detail")
    expect(detail.locator(".impact-list li").first).to_be_visible()
    assert detail.locator(".impact-list li").count() == min(200, reach[0])


def test_model_page_and_lists_show_lineage(page: "Page", reports: t.Dict[str, str]) -> None:
    _open(page, reports["scale"], "model?name=demo.mart_00.model_0000")
    panel = page.locator(".lineage-panel")
    expect(panel).to_contain_text("source_00")
    expect(panel).to_contain_text("model_0300")
    expect(panel.get_by_role("link", name="Open lineage")).to_have_attribute(
        "href", re.compile("lineage\\?model=demo")
    )
    expect(page.locator(".facts")).to_contain_text("Downstream")

    page.evaluate("location.hash = 'overview'")
    expect(page.locator(".list-head")).to_contain_text("Downstream")
    page.evaluate("location.hash = 'models'")
    expect(page.locator(".list-head")).to_contain_text("Downstream")
