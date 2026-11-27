"""Tests: the report's embedded JSON payload (``report-data``) and the synthetic scale fixture."""

from __future__ import annotations

import json
import typing as t

from scale_fixture import build_scale_data

if t.TYPE_CHECKING:
    from sqlmesh_observability.reader import ReportData


def test_fixture_builds_seeded_scale_data() -> None:
    data = build_scale_data(n=300, days=30, failing=3, warning=4, anomalous=5)

    assert len(data.models) == 300
    assert data.failing_model_count == 3
    assert data.warning_model_count == 4
    assert len(data.anomalies) == 5
    assert {m.owner for m in data.models} == {f"team-{i:02d}" for i in range(20)}
    assert data.monitors == ("volume", "freshness", "schema", "columns")
    # Seeded: the same arguments give the same data.
    again = build_scale_data(n=300, days=30, failing=3, warning=4, anomalous=5)
    assert again.models[42].runs == data.models[42].runs


def decode(document: str) -> t.Dict[str, t.Any]:
    """The payload embedded in a rendered report."""
    marker = '<script type="application/json" id="report-data">'
    assert document.count(marker) == 1
    start = document.index(marker) + len(marker)
    end = document.index("</script>", start)
    return t.cast(t.Dict[str, t.Any], json.loads(document[start:end]))


def payload_for(data: "ReportData", **kwargs: t.Any) -> t.Dict[str, t.Any]:
    from sqlmesh_observability.payload import build_payload

    return build_payload(data, **kwargs)


def _model(payload: t.Dict[str, t.Any], leaf: str) -> t.Dict[str, t.Any]:
    return next(m for m in payload["models"] if m["n"].endswith(leaf))


def test_payload_is_versioned() -> None:
    payload = payload_for(build_scale_data(n=3, days=10, failing=1, warning=0, anomalous=0))

    assert payload["schema_version"] == 2
    assert payload["environment"] == "prod"
    assert payload["generated_at"].startswith("2024-03-31")
    assert payload["state_available"] is True
    assert payload["monitors"] == ["volume", "freshness", "schema", "columns"]


def test_payload_covers_failing_and_passing_models() -> None:
    payload = payload_for(build_scale_data(n=3, days=10, failing=1, warning=0, anomalous=0))

    failing, passing = _model(payload, "model_0000"), _model(payload, "model_0001")
    assert failing["st"] == "fail" and passing["st"] == "pass"
    assert payload["owners"][failing["o"]] == "team-00"
    assert ["payment_id", "INT"] in failing["c"]
    assert len(failing["h"][1]) == 10 and len(failing["r"][1]) == 10
    assert [payload["metrics"][s[0]] for s in failing["m"]] == [
        "row_count",
        "freshness_lag_seconds",
        "null_rate",
    ]

    # Audit history: latest run per (use, interval), newest first; the failing use links to its
    # incident, which carries the SQL and both sample rows.
    uses = payload["audits"]
    fail_rows = [row for row in failing["a"] if row[2] == 1]
    assert len(fail_rows) == 1
    day, use, _, violations, blocking, incident_index = fail_rows[0]
    assert payload["dates"][day] == "2024-03-31"
    assert uses[use] == ["not_null", "columns := (payment_id)"]
    assert violations == 4 and blocking == 1
    incident = payload["incidents"][incident_index]
    assert incident["model"] == payload["models"].index(failing)
    assert "SELECT" in incident["sql"]
    assert incident["samples"] == [{"id": 1, "payment_id": None}]
    assert all(row[5] == -1 for row in passing["a"])


def test_payload_lists_open_issues_in_severity_order() -> None:
    payload = payload_for(build_scale_data(n=30, days=30, failing=2, warning=3, anomalous=4))

    kinds = [issue["kind"] for issue in payload["issues"]]
    assert kinds == ["blocking"] * 2 + ["anomaly"] * 4 + ["warning"] * 3
    anomaly = payload["issues"][2]
    assert anomaly["signal"] == "row_count" and anomaly["value"] > anomaly["hi"]


def test_payload_summary_and_signals() -> None:
    payload = payload_for(build_scale_data(n=30, days=30, failing=2, warning=3, anomalous=4))

    assert payload["summary"]["models"] == {"pass": 21, "warn": 7, "fail": 2}
    assert payload["summary"]["monitored"] == {"monitored": 30, "unmonitored": 0}
    assert payload["signals"]["volume"] == {"pass": 26, "warn": 4, "fail": 0}
    assert payload["signals"]["audits"] == {"pass": 30, "warn": 3, "fail": 2}
    assert payload["signals"]["schema"] == {"pass": 29, "warn": 1, "fail": 0}


def test_signal_without_monitor_is_not_configured() -> None:
    from dataclasses import replace

    data = build_scale_data(n=3, days=10, failing=0, warning=0, anomalous=0)
    data = replace(
        data,
        monitors=("volume",),
        models=tuple(
            replace(
                m,
                metrics=tuple(s for s in m.metrics if s.metric_name == "row_count"),
                schema_changes=(),
            )
            for m in data.models
        ),
    )
    payload = payload_for(data)

    assert payload["signals"]["freshness"] is None
    assert payload["signals"]["columns"] is None
    assert payload["signals"]["schema"] is None
    assert payload["signals"]["volume"] == {"pass": 3, "warn": 0, "fail": 0}


def test_daily_trend_has_one_point_per_day_with_data() -> None:
    from dataclasses import replace

    data = build_scale_data(n=5, days=30, failing=1, warning=0, anomalous=0)
    keep = {"2024-03-30 00:00:00", "2024-03-31 00:00:00"}
    data = replace(
        data,
        models=tuple(
            replace(m, audits=tuple(a for a in m.audits if a.interval_start in keep))
            for m in data.models
        ),
    )
    trend = payload_for(data)["trend"]

    assert [payload_for(data)["dates"][row[0]] for row in trend] == ["2024-03-30", "2024-03-31"]
    assert trend[-1][1:3] == [6, 1]  # 5 passing not_null plus 1 failing; 1 blocking failure


def test_history_cap_keeps_the_latest_intervals_and_flags() -> None:
    from sqlmesh_observability.payload import undelta

    payload = payload_for(
        build_scale_data(n=10, days=90, failing=1, warning=0, anomalous=1), history=30
    )
    spiked = _model(payload, "model_0001")
    volume = spiked["m"][0]
    dates = [payload["dates"][i] for i in undelta(volume[3])]

    assert len(dates) == 30 and dates[0] == "2024-03-02" and dates[-1] == "2024-03-31"
    assert volume[7][-1] & 1  # the last-day spike is still flagged, scored on 90 days
    assert len(spiked["h"][1]) == 30 and len(spiked["r"][1]) == 30
    assert {payload["dates"][row[0]] for row in spiked["a"]} == set(dates)
    assert payload["history"] == 30


def test_cli_takes_history_option() -> None:
    from sqlmesh_observability.cli import _parser

    for command in ("report", "serve"):
        assert _parser().parse_args([command]).history == 30, command
        assert _parser().parse_args([command, "--history", "60"]).history == 60, command


HOSTILE = "</script><img src=x onerror=alert(1)> &amp;"


def test_hostile_strings_cannot_end_the_payload_script() -> None:
    from dataclasses import replace

    from sqlmesh_observability.report import render_report

    data = build_scale_data(n=2, days=10, failing=1, warning=0, anomalous=0)
    model = data.failing_models[0]
    bad = replace(
        model.failing_blocking_audits[0], samples=('{"v": "%s"}' % HOSTILE.replace(" ", "\\u2028"),)
    )
    model = replace(
        model, failing_blocking_audits=(bad,), audits=(*model.audits[:-1], bad), owner=HOSTILE
    )
    data = replace(
        data,
        failing_models=(model,),
        models=(model, *data.models[1:]),
        incidents=(bad,),
    )
    document = render_report(data)

    payload_start = document.index('<script type="application/json" id="report-data">')
    payload_end = document.index("</script>", payload_start)
    assert "<" not in document[payload_start + 10 : payload_end].split(">", 1)[1]
    assert "<img" not in document.replace("&lt;img", "")
    decoded = decode(document)
    assert decoded["owners"][0] == HOSTILE
    assert decoded["incidents"][0]["samples"] == [{"v": HOSTILE}]


def test_payload_never_holds_connection_settings(tmp_path: "t.Any") -> None:
    from sqlmesh_observability.report import render_report

    data = build_scale_data(n=2, days=5, failing=1, warning=0, anomalous=0)
    document = render_report(data)
    for secret in ("password", "token", "DuckDBConnectionConfig", "database"):
        assert secret not in json.dumps(decode(document)).lower()


SIZE_BUDGET = 3_000_000  # bytes, for the whole page at 1,500 models with the default cap


def test_scale_report_stays_under_the_size_budget() -> None:
    from sqlmesh_observability.report import render_report

    document = render_report(build_scale_data(n=1500, days=90))
    assert len(document.encode("utf-8")) < SIZE_BUDGET


def test_fallback_is_readable_without_javascript() -> None:
    from html.parser import HTMLParser

    from sqlmesh_observability.report import render_report

    data = build_scale_data(n=30, days=10, failing=2, warning=3, anomalous=4)
    document = render_report(data)

    class Visible(HTMLParser):
        text: t.List[str] = []
        skip = 0

        def handle_starttag(self, tag: str, attrs: t.Any) -> None:
            self.skip += tag in ("script", "style")

        def handle_endtag(self, tag: str) -> None:
            self.skip -= tag in ("script", "style")

        def handle_data(self, data: str) -> None:
            if not self.skip and data.strip():
                self.text.append(data.strip())

    parser = Visible()
    parser.feed(document)
    text = " ".join(parser.text)
    assert "prod: 2 failing models" in text
    assert "2 failing models" in text and "3 models with warnings" not in text  # 7 warn models
    assert "30 passing audits" in text and "9 open issues" in text
    for issue_model in ("model_0000", "model_0001", "model_0002", "model_0005", "model_0008"):
        assert issue_model in text
    assert "not_null columns := (payment_id)" in text
    assert "not_null_non_blocking columns := (status)" in text


def test_payload_is_schema_version_2_with_lineage() -> None:
    payload = payload_for(build_scale_data(n=30, days=10, failing=2, warning=3, anomalous=4))
    assert payload["schema_version"] == 2

    lineage = payload["lineage"]
    names = [m["n"] for m in payload["models"]] + lineage["extra"]
    # Sources have no observability rows: they are lineage-only nodes after the models, and
    # they don't change the summary's model counts.
    assert lineage["extra"] == [f"demo.raw.source_{i:02d}" for i in range(20)]
    assert sum(payload["summary"]["models"].values()) == 30
    assert len(lineage["parents"]) == len(lineage["kind"]) == len(lineage["impact"]) == 50
    assert lineage["kinds"][lineage["kind"][names.index("demo.raw.source_00")]] == "EXTERNAL"

    model_0 = names.index("demo.mart_00.model_0000")
    assert [names[p] for p in lineage["parents"][model_0]] == ["demo.raw.source_00"]
    # source_00 feeds model_0000 and model_0020, which feed nothing at this size.
    assert lineage["impact"][names.index("demo.raw.source_00")] == 2


def test_impact_counts_transitive_descendants_and_issues_carry_them() -> None:
    payload = payload_for(build_scale_data(n=1500, days=10))
    lineage = payload["lineage"]
    names = [m["n"] for m in payload["models"]] + lineage["extra"]
    children: t.Dict[int, t.List[int]] = {}
    for child, parents in enumerate(lineage["parents"]):
        for parent in parents:
            children.setdefault(parent, []).append(child)

    def descendants(node: int) -> int:
        seen: t.Set[int] = set()
        stack = list(children.get(node, []))
        while stack:
            current = stack.pop()
            if current not in seen:
                seen.add(current)
                stack.extend(children.get(current, []))
        return len(seen)

    fan_out = names.index("demo.mart_03.model_0300")
    assert len(children[fan_out]) == 400
    for node in (
        fan_out,
        names.index("demo.mart_00.model_0000"),
        names.index("demo.raw.source_00"),
    ):
        assert lineage["impact"][node] == descendants(node)
    for issue in payload["issues"]:
        assert issue["impact"] == lineage["impact"][issue["model"]]
    assert max(i["impact"] for i in payload["issues"] if i["kind"] == "blocking") > 0


def test_lineage_is_empty_without_state() -> None:
    from dataclasses import replace

    data = replace(build_scale_data(n=3, days=5), lineage=())
    lineage = payload_for(data)["lineage"]
    assert lineage["extra"] == [] and lineage["parents"] == [[], [], []]
    assert lineage["kind"] == [-1, -1, -1] and lineage["impact"] == [0, 0, 0]


def test_fallback_names_each_issues_direct_downstream_models() -> None:
    import re

    from sqlmesh_observability.report import render_report

    document = render_report(build_scale_data(n=1500, days=10))
    failures = document[document.index('id="overview-failures"') :]
    failures = failures[: failures.index("</tbody>")]
    row = re.search(r"<tr><td><code>demo\.mart_00\.model_0000</code>.*?</tr>", failures, re.S)
    assert row, "model_0000's issue row"
    assert "Downstream" in document[: document.index('id="overview-failures"')]  # column head
    assert "mart_03.model_0300" in row.group(0)
