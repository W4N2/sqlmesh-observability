"""Self-contained HTML report: the Graphite Console dashboard.

Renders :class:`~sqlmesh_observability.reader.ReportData` to one offline HTML5 file laid out as a
working console: a nav rail (views and models), a top bar, the work area, and a run-log rail.
Colour means state and nothing else. Charts are inline SVG (``charts.py``).

Every view is plain HTML in the document. The one inline script only adds view switching, model
pages, filters, and the light/dark toggle by showing and hiding what is already there, so the
file reads top to bottom without JavaScript and static parsers see every word.
"""

from __future__ import annotations

import hashlib
import html
import json
import typing as t
from pathlib import Path

from sqlmesh_observability import charts
from sqlmesh_observability.reader import (
    AuditRecord,
    InvocationRecord,
    MetricSeries,
    ModelReport,
    ReportData,
    SchemaChange,
    audit_key,
)

_METRIC_LABELS = {
    "row_count": "Row count",
    "freshness_lag_seconds": "Freshness lag (seconds)",
    "null_rate": "Null rate",
    "mean": "Mean",
}

_LIGHT_TOKENS = """
    color-scheme: light;
    --nav: #eceef1; --ground: #f6f7f9; --panel: #ffffff; --raised: #eef0f3; --hover: #f1f3f5;
    --seam: #dadee3; --seam-strong: #c5cbd2;
    --t1: #15181c; --t2: #3f464e; --t3: #5d656e;
    --red: #c8372b; --amber: #8a5a00; --green: #1d7643; --blue: #1f5fbf;
    --red-bg: rgba(200,55,43,.1); --amber-bg: rgba(181,120,0,.12);
    --green-bg: rgba(29,118,67,.1); --blue-bg: rgba(31,95,191,.1);
    --plate: #15181c; --plate-ink: #f6f7f9; --plate-ink-2: #c3c9cf;
    --on-state: #ffffff;
    --series: #1f5fbf; --band: rgba(31,95,191,.13);
"""

_CSS = (
    """
:root {
  color-scheme: dark;
  --nav: #121417; --ground: #16181b; --panel: #1e2125; --raised: #272b30; --hover: #23272c;
  --seam: #2e3238; --seam-strong: #3a3f46;
  --t1: #e6e8ea; --t2: #b4bac1; --t3: #8b929a;
  --red: #ff6b5e; --amber: #f2b84b; --green: #5fd38d; --blue: #7cb3ff;
  --red-bg: rgba(255,107,94,.14); --amber-bg: rgba(242,184,75,.14);
  --green-bg: rgba(95,211,141,.13); --blue-bg: rgba(124,179,255,.14);
  --plate: #e6e8ea; --plate-ink: #121417; --plate-ink-2: #3d434a;
  --on-state: #121417;
  --series: #7cb3ff; --band: rgba(124,179,255,.16);
}
@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) {"""
    + _LIGHT_TOKENS
    + """  }
}
:root[data-theme="light"] {"""
    + _LIGHT_TOKENS
    + """}
* { box-sizing: border-box; }
html { background: var(--ground); }
* { scrollbar-color: var(--seam-strong) transparent; }
body {
  margin: 0; background: var(--ground); color: var(--t1);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue",
    sans-serif;
  -webkit-font-smoothing: antialiased;
}
::selection { background: var(--blue-bg); color: var(--t1); }
:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; border-radius: 4px; }
a { color: var(--blue); text-decoration: none; text-underline-offset: 3px; }
a:hover { text-decoration: underline; }
code, .mono, pre, td.num {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
}
code { font-size: 0.92em; }
.muted { color: var(--t3); }
.icon { width: 16px; height: 16px; flex: none; stroke: currentColor; fill: none;
  stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; }

/* Shell */
.app { display: grid; grid-template-columns: 232px minmax(0, 1fr) 320px; min-height: 100vh; }
.sidebar {
  position: sticky; top: 0; height: 100vh; overflow-y: auto; background: var(--nav);
  border-right: 1px solid var(--seam); padding: 16px 12px 24px;
  display: flex; flex-direction: column; gap: 4px;
}
.brand { padding: 2px 10px 14px; border-bottom: 1px solid var(--seam); margin-bottom: 10px; }
.brand-name { font: 600 15px ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--t1); }
.brand-meta { display: block; color: var(--t3); font-size: 12.5px; margin-top: 2px; }
.views { list-style: none; margin: 0; padding: 0; display: grid; gap: 1px; }
.views a {
  display: flex; align-items: center; gap: 10px; padding: 7px 10px; border-radius: 6px;
  color: var(--t2); font-weight: 500;
}
.views a:hover { background: var(--hover); color: var(--t1); text-decoration: none; }
.views a[aria-current="page"] { background: var(--raised); color: var(--t1); }
.views .count { margin-left: auto; }
.count {
  font: 600 11.5px ui-monospace, SFMono-Regular, Menlo, monospace; padding: 1px 7px;
  border-radius: 4px; background: var(--raised); color: var(--t2);
}
.count.red { background: var(--red-bg); color: var(--red); }
.count.amber { background: var(--amber-bg); color: var(--amber); }
.side-head {
  margin: 18px 10px 8px; font-size: 11.5px; font-weight: 600; letter-spacing: .07em;
  text-transform: uppercase; color: var(--t3);
}
.guide { display: block; font: 12px ui-monospace, Menlo, monospace; letter-spacing: 0;
  text-transform: none; color: var(--t3); margin-top: 2px; font-weight: 400;
    overflow-wrap: anywhere; }
.filters { display: grid; gap: 8px; padding: 0 10px; }
.filters label { display: grid; gap: 3px; font-size: 12px; color: var(--t3); }
.filters input, .filters select, .btn {
  font: inherit; font-size: 13px; color: var(--t1); background: var(--panel);
  border: 1px solid var(--seam); border-radius: 6px; padding: 6px 9px;
}
.filters input::placeholder { color: var(--t3); }
.filter-note { margin: 6px 10px 2px; font-size: 12px; color: var(--t3); min-height: 1em; }
.model-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 1px; }
.model-list a {
  display: grid; grid-template-columns: 8px 1fr; gap: 10px; align-items: center;
  padding: 6px 10px; border-radius: 6px; color: var(--t2);
}
.model-list a:hover { background: var(--hover); color: var(--t1); text-decoration: none; }
.model-list a[aria-current="page"] { background: var(--raised); color: var(--t1); }
.model-list .name { font: 12.5px ui-monospace, SFMono-Regular, Menlo, monospace;
  overflow-wrap: anywhere; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); flex: none;
  display: inline-block; }
.dot.fail, .dot.failure, .dot.error { background: var(--red); }
.dot.warn, .dot.unfinished { background: var(--amber); }

.work { min-width: 0; display: flex; flex-direction: column; }
.topbar {
  position: sticky; top: 0; z-index: 2; display: flex; align-items: center; gap: 8px;
  min-height: 56px; padding: 10px 24px; background: var(--ground);
  border-bottom: 1px solid var(--seam); flex-wrap: wrap;
}
.topbar h1 { margin: 0 10px 0 0; font-size: 17px; font-weight: 600; }
.chip { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; color: var(--t3);
  border: 1px solid var(--seam); border-radius: 6px; padding: 3px 9px; }
.chip code { color: var(--t1); }
.topbar .spacer { margin-left: auto; }
.btn { display: inline-flex; align-items: center; gap: 7px; cursor: pointer; color: var(--t2); }
.btn:hover { color: var(--t1); border-color: var(--seam-strong); }
.no-js .js-only { display: none; }
main { padding: 20px 24px 56px; display: grid; gap: 32px; align-content: start; }

/* Panels */
.view { display: grid; gap: 16px; align-content: start; scroll-margin-top: 72px; min-width: 0; }
.view > h2 { margin: 0; font-size: 15px; font-weight: 600; display: flex; align-items: center;
  gap: 10px; }
.js .view > h2 { display: none; }
.panel { background: var(--panel); border: 1px solid var(--seam); border-radius: 8px;
  min-width: 0; }
.panel-head {
  display: flex; align-items: center; gap: 10px; padding: 11px 16px;
    border-bottom: 1px solid var(--seam);
  flex-wrap: wrap;
}
.panel-head h3 { margin: 0; font-size: 12px; font-weight: 600; letter-spacing: .07em;
  text-transform: uppercase; color: var(--t1); }
.panel-head .aside { margin-left: auto; color: var(--t3); font-size: 12.5px; }
.panel-body { padding: 14px 16px; }
.empty { color: var(--t3); margin: 0; padding: 14px 16px; }

/* Tiles: each count is a link to the rows behind it */
.tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); background: var(--panel);
  border: 1px solid var(--seam); border-radius: 8px; overflow: hidden; }
.tile { display: block; padding: 14px 16px 15px; border-right: 1px solid var(--seam);
  color: var(--t2); }
.tile:last-child { border-right: 0; }
.tile:hover { background: var(--hover); text-decoration: none; }
.tile-label { font-size: 12px; font-weight: 600; letter-spacing: .06em;
  text-transform: uppercase; color: var(--t3); }
.tile-value { display: block; font: 600 32px/1.15 ui-monospace, SFMono-Regular, Menlo, monospace;
  color: var(--t1); margin: 6px 0 2px; font-variant-numeric: tabular-nums; }
.tile-value.red { color: var(--red); } .tile-value.amber { color: var(--amber); }
.tile-value.green { color: var(--green); }
.tile-note { display: block; font-size: 12.5px; color: var(--t3); overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }

/* The plate: one reversed element per screen, for the single most urgent item */
.plate {
  display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 14px; align-items: center;
  margin: 14px 16px 10px; padding: 13px 16px; border-radius: 6px; background: var(--plate);
    color: var(--plate-ink);
}
.plate > .pill { justify-self: start; }
.plate-when { font: 12.5px ui-monospace, SFMono-Regular, Menlo, monospace;
  color: var(--plate-ink-2);
  white-space: nowrap; }
tr.plate-row td { padding: 12px 16px 8px; }
tr.plate-row:hover td { background: transparent; }
tr.plate-row .plate { margin: 0; }
td a.name, td a.sig { color: var(--t1); }
td a.sig { color: var(--t2); }
td a.name:hover, td a.sig:hover { color: var(--blue); }
.panel-foot, .rail-foot { display: block; padding: 10px 16px; border-top: 1px solid var(--seam);
  font-size: 13px; font-weight: 500; }
.rail-foot { border-top: 0; padding: 10px 6px; }
.st { font: 600 12px ui-monospace, SFMono-Regular, Menlo, monospace; margin-left: 4px; }
.st.fail { color: var(--red); } .st.warn { color: var(--amber); }
.dot.pass { background: var(--green); }
svg.compact .cell.s-pass { fill: var(--raised); }
svg.compact .cell.s-skip { fill: var(--seam); }
.js #models[data-mode="single"] .model-head h3 { display: none; }
.plate-title { font-weight: 600; font-size: 15px; overflow-wrap: anywhere; }
.plate-title .args { color: var(--plate-ink-2); }
.plate-detail { color: var(--plate-ink-2); font-size: 13px; }
.plate a { color: var(--plate-ink); text-decoration: underline; font-weight: 500;
  white-space: nowrap; }
.plate .pill.red { background: var(--red); color: var(--on-state); }
.plate .pill.amber { background: var(--amber); color: var(--on-state); }

/* Tables */
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
th { text-align: left; font-size: 11.5px; font-weight: 600; letter-spacing: .06em;
  text-transform: uppercase;
  color: var(--t3); padding: 9px 16px; border-bottom: 1px solid var(--seam); white-space: nowrap; }
td { padding: 9px 16px; border-bottom: 1px solid var(--seam); color: var(--t2);
  vertical-align: top; }
tbody:last-child tr:last-child td { border-bottom: 0; }
tbody tr:hover td { background: var(--hover); }
td.strong, td strong { color: var(--t1); font-weight: 500; }
td.num, th.num { text-align: right; }
td.mono, td.num { white-space: nowrap; }
td .strong { color: var(--t1); font-weight: 500; }
td .exp { display: block; font-size: 12px; color: var(--t3); }
tbody.group tr.group-head td {
  background: var(--ground); color: var(--t3); font-size: 11.5px; font-weight: 600;
    letter-spacing: .06em;
  text-transform: uppercase; padding: 7px 16px;
}
tbody.group tr.group-head:hover td { background: var(--ground); }
tbody.group td.none { color: var(--t3); }
.args { color: var(--t3); font-size: 12.5px; }
.pill { display: inline-block; font: 600 11.5px ui-monospace, SFMono-Regular, Menlo, monospace;
  padding: 2px 7px; border-radius: 4px; white-space: nowrap; background: var(--raised);
    color: var(--t2); }
.pill.red, .pill.fail, .pill.error, .pill.failure, .pill.removed,
.pill.blocking { background: var(--red-bg);
  color: var(--red); }
.pill.amber, .pill.warn, .pill.anomaly, .pill.unfinished,
.pill.retyped { background: var(--amber-bg);
  color: var(--amber); }
.pill.green, .pill.pass, .pill.success, .pill.added { background: var(--green-bg);
  color: var(--green); }

/* Model pages */
.model { display: grid; gap: 14px; scroll-margin-top: 72px; min-width: 0; }
.model + .model { border-top: 1px solid var(--seam); padding-top: 24px; }
.js .model + .model { border-top: 0; padding-top: 0; }
.model-head { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 14px; }
.model-head h3 { margin: 0; font: 600 17px ui-monospace, SFMono-Regular, Menlo, monospace;
  overflow-wrap: anywhere; }
.facts { display: flex; flex-wrap: wrap; gap: 6px 20px; color: var(--t3); font-size: 13px; }
.facts strong { color: var(--t1); font-weight: 500; }
.cols { display: flex; flex-wrap: wrap; gap: 6px; }
.col { font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; padding: 2px 7px;
  border-radius: 4px;
  background: var(--raised); color: var(--t3); }
.col b { color: var(--t1); font-weight: 500; }
.grid-2 { display: grid; grid-template-columns: minmax(0, 1fr); gap: 14px; }
.viz svg { display: block; width: 100%; max-width: 100%; height: auto; overflow: visible; }
.viz svg.strip { width: auto; max-width: 100%; }
td .viz svg.strip { height: 18px; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 14px; list-style: none; margin: 10px 0 0;
  padding: 0;
  font-size: 12.5px; color: var(--t3); }
.legend li { display: inline-flex; align-items: center; gap: 6px; }
.swatch { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
.swatch.s-pass { background: var(--green); } .swatch.s-warn { background: var(--amber); }
.swatch.s-fail { background: var(--red); } .swatch.s-error { background: var(--red);
  outline: 2px solid var(--amber); outline-offset: -2px; }
.swatch.s-skip { background: var(--seam-strong); }
.swatch.band { background: var(--band); outline: 1px solid var(--series); outline-offset: -1px; }
.swatch.line { height: 2px; background: var(--series); }
.notes { margin: 10px 0 0; padding: 0; list-style: none; display: grid; gap: 4px; }
.notes li { color: var(--t2); font-size: 13px; display: flex; gap: 8px; align-items: baseline; }
.notes li .pill { flex: none; }
.sub-head { margin: 16px 0 6px; font-size: 12px; font-weight: 600; letter-spacing: .06em;
  text-transform: uppercase; color: var(--t3); }
details > summary { cursor: pointer; color: var(--blue); font-size: 13px; font-weight: 500;
  padding: 10px 16px; }
details > summary:hover { color: var(--t1); }
details[open] > summary { border-bottom: 1px solid var(--seam); }
.panel > details:not(:first-child) > summary { border-top: 1px solid var(--seam); }
pre { margin: 0; padding: 12px 16px; overflow-x: auto; font-size: 12.5px; line-height: 1.55;
  color: var(--t1); background: var(--ground); }

/* Incidents */
.incident { scroll-margin-top: 72px; }
.incident .panel-head h3 { text-transform: none; letter-spacing: 0; font-size: 14px; }
.incident .panel-head h3 code { font-size: 13px; }
.kv { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 6px 20px; margin: 0;
  padding: 14px 16px; font-size: 13.5px; }
.kv dt { color: var(--t3); }
.kv dd { margin: 0; color: var(--t1); overflow-wrap: anywhere; }
.samples-head { margin: 0; padding: 10px 16px 0; font-size: 12px; font-weight: 600;
  letter-spacing: .06em;
  text-transform: uppercase; color: var(--t3); border-top: 1px solid var(--seam); }
.banner { display: flex; gap: 10px; align-items: flex-start; background: var(--amber-bg);
  color: var(--t1);
  border: 1px solid var(--seam); border-radius: 8px; padding: 12px 16px; }
.banner .icon { color: var(--amber); margin-top: 2px; }

/* Charts (inline SVG from charts.py) */
svg .grid { stroke: var(--seam); stroke-width: 1; }
svg .axis { stroke: var(--seam-strong); stroke-width: 1; }
svg .tick { fill: var(--t3); font: 11px ui-monospace, SFMono-Regular, Menlo, monospace; }
svg .band { fill: var(--band); stroke: none; }
svg .line { fill: none; stroke: var(--series); stroke-width: 2; stroke-linejoin: round;
  stroke-linecap: round; }
svg .point { fill: var(--series); stroke: var(--panel); stroke-width: 2; }
svg .anomaly { fill: var(--amber); stroke: var(--panel); stroke-width: 2.5; }
svg .hit { fill: transparent; stroke: none; }
svg .hit:hover { stroke: var(--t3); stroke-width: 1; }
svg .duration { fill: none; stroke: var(--series); stroke-width: 2; stroke-linejoin: round; }
svg .spark-point { fill: var(--series); }
svg .cell.s-pass { fill: var(--green); } svg .cell.s-warn { fill: var(--amber); }
svg .cell.s-fail { fill: var(--red); } svg .cell.s-error { fill: var(--red);
  stroke: var(--amber); stroke-width: 2; }
svg .cell.s-skip { fill: var(--seam-strong); }
svg .cell-letter { font: 700 10px ui-monospace, Menlo, monospace; fill: var(--on-state);
  pointer-events: none; }
svg .cell-letter.s-skip { fill: var(--t1); }

/* Run log rail */
.rail { position: sticky; top: 0; height: 100vh; overflow-y: auto; background: var(--nav);
  border-left: 1px solid var(--seam); padding: 18px 16px; }
.rail h2 { margin: 0 0 8px; font-size: 12px; font-weight: 600; letter-spacing: .07em;
  text-transform: uppercase; color: var(--t3); }
.status-box { border: 1px solid var(--seam); border-radius: 8px; padding: 12px 14px;
  background: var(--panel);
  margin-bottom: 22px; display: grid; gap: 4px; }
.status-box .line1 { display: flex; align-items: center; gap: 8px; font-weight: 600; }
.status-box .muted { font-size: 12.5px; }
.runs { list-style: none; margin: 0; padding: 0; }
.runs li a { display: grid; grid-template-columns: 8px minmax(0, 1fr) auto; gap: 10px;
  align-items: baseline;
  padding: 10px 6px; border-bottom: 1px solid var(--seam); color: var(--t2); border-radius: 4px; }
.runs li a:hover { background: var(--hover); text-decoration: none; }
.runs .what { color: var(--t1); font-weight: 500; }
.runs .detail { display: block; font-size: 12.5px; color: var(--t3); }
.runs time { font: 12px ui-monospace, Menlo, monospace; color: var(--t3); }

/* Without JavaScript every view shows in order; with it, one view (or one model) at a time. */
.js .view[data-active="false"], .js .model[data-active="false"] { display: none; }
.js #models[data-mode="single"] .models-index { display: none; }
.js #models[data-mode="index"] .model { display: none; }
@media (prefers-reduced-motion: no-preference) {
  .js .view[data-active="true"] { animation: enter .2s cubic-bezier(.16, 1, .3, 1); }
  @keyframes enter { from { opacity: 0; transform: translateY(6px); } to { opacity: 1;
    transform: none; } }
}
@media (max-width: 1240px) {
  .app { grid-template-columns: 232px minmax(0, 1fr); }
  .rail { grid-column: 2; position: static; height: auto; border-left: 0;
    border-top: 1px solid var(--seam); }
}
@media (max-width: 860px) {
  .app { grid-template-columns: minmax(0, 1fr); }
  .work { order: 1; } .sidebar { order: 2; } .rail { order: 3; }
  .sidebar, .rail { position: static; height: auto; grid-column: auto; border: 0;
    border-top: 1px solid var(--seam); }
  .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .tile:nth-child(2) { border-right: 0; }
  .tile:nth-child(-n+2) { border-bottom: 1px solid var(--seam); }
  main { padding: 16px; }
  .topbar { position: static; padding: 10px 16px; }
  .plate { grid-template-columns: minmax(0, 1fr); }
  .incidents-table th:nth-child(3), .incidents-table td:nth-child(3) { display: none; }
  .incidents-table .args { display: none; }
  .incidents-table th, .incidents-table td { padding-left: 10px; padding-right: 10px; }
}
"""
)

# View switching, model pages, filters, guide words, theme. Progressive: without it every view
# is visible in document order.
_SCRIPT = """
(function () {
  var root = document.documentElement;
  root.classList.remove('no-js'); root.classList.add('js');
  var KEY = 'sqlmesh-observe-theme';
  function stored() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function store(v) { try { localStorage.setItem(KEY, v); } catch (e) {} }
  function currentTheme() {
    var t = root.getAttribute('data-theme');
    if (t) return t;
    var light = window.matchMedia && matchMedia('(prefers-color-scheme: light)').matches;
    return light ? 'light' : 'dark';
  }
  var toggle = document.getElementById('theme-toggle');
  function paintToggle() {
    if (!toggle) return;
    var next = currentTheme() === 'dark' ? 'light' : 'dark';
    toggle.querySelector('.label').textContent = next === 'light' ? 'Light' : 'Dark';
    toggle.setAttribute('aria-label', 'Switch to ' + next + ' theme');
  }
  var saved = stored();
  if (saved === 'light' || saved === 'dark') root.setAttribute('data-theme', saved);
  paintToggle();
  if (toggle) toggle.addEventListener('click', function () {
    var next = currentTheme() === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next); store(next); paintToggle();
  });

  var views = Array.prototype.slice.call(document.querySelectorAll('.view'));
  var models = document.getElementById('models');
  var title = document.getElementById('view-title');
  function show(viewId, modelId, focusId) {
    views.forEach(function (v) { v.setAttribute('data-active', v.id === viewId ? 'true' : 'false');
      });
    var single = viewId === 'models' && modelId;
    if (models) {
      models.setAttribute('data-mode', single ? 'single' : 'index');
      models.querySelectorAll('.model').forEach(function (m) {
        m.setAttribute('data-active', !single || m.id === modelId ? 'true' : 'false');
      });
    }
    document.querySelectorAll('[data-nav]').forEach(function (a) {
      var on = a.getAttribute('data-nav') === (single ? modelId : viewId);
      if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
    var active = document.getElementById(single ? modelId : viewId);
    if (title && active) title.textContent = active.getAttribute('data-title') || title.textContent;
    var env = document.body.getAttribute('data-env');
    document.title = (title ? title.textContent + ' \\u00b7 ' : '') + env;
    var target = focusId && document.getElementById(focusId);
    if (target && target !== active) target.scrollIntoView({ block: 'start' });
    else window.scrollTo(0, 0);
  }
  function route() {
    var id = decodeURIComponent(location.hash.slice(1)) ||
      document.body.getAttribute('data-start') || '';
    var el = id && document.getElementById(id);
    if (!el) return show('overview');
    if (el.classList.contains('view')) return show(id);
    var model = el.closest('.model');
    if (model) return show('models', model.id, el === model ? null : id);
    var view = el.closest('.view');
    show(view ? view.id : 'overview', null, id);
  }
  window.addEventListener('hashchange', route);
  route();

  var search = document.getElementById('model-search');
  var status = document.getElementById('status-filter');
  var owner = document.getElementById('owner-filter');
  var note = document.getElementById('filter-note');
  var guide = document.getElementById('model-guide');
  function applyFilters() {
    var q = (search.value || '').trim().toLowerCase();
    var shown = [], total = 0;
    document.querySelectorAll('[data-model]').forEach(function (el) {
      var ok = (!q || el.getAttribute('data-model').toLowerCase().indexOf(q) !== -1) &&
        (!status.value || el.getAttribute('data-status') === status.value) &&
        (!owner.value || el.getAttribute('data-owner') === owner.value);
      el.hidden = !ok;
      if (el.tagName === 'LI') { total += 1; if (ok) shown.push(el.getAttribute('data-short')); }
    });
    note.textContent = shown.length === total
      ? '' : shown.length + ' of ' + total + ' models shown';
    if (guide) guide.textContent = shown.length
      ? shown[0] + ' \\u2192 ' + shown[shown.length - 1] : 'no models match';
  }
  if (search && status && owner && note) {
    [search, status, owner].forEach(function (el) {
      el.addEventListener('input', applyFilters); el.addEventListener('change', applyFilters);
    });
  }
})();
"""

# Authored icons: 16px grid, 1.6 stroke, round joins. Referenced with <use>.
_ICONS = {
    "dashboard": '<rect x="2" y="2" width="5" height="5" rx="1"/>'
    '<rect x="9" y="2" width="5" height="5" rx="1"/>'
    '<rect x="2" y="9" width="5" height="5" rx="1"/>'
    '<rect x="9" y="9" width="5" height="5" rx="1"/>',
    "incident": '<path d="M8 2.2 14.2 13H1.8Z"/><path d="M8 6.5v3"/><path d="M8 11.4v.1"/>',
    "tests": '<rect x="2" y="2" width="12" height="12" rx="2"/><path d="m5 8.2 2 2 4-4.4"/>',
    "runs": '<circle cx="8" cy="8" r="6"/><path d="M8 4.8V8l2.2 1.4"/>',
    "schema": '<rect x="2" y="2.5" width="12" height="11" rx="1.5"/><path d="M2 6h12M6.5 6v7.5"/>',
    "models": '<path d="M8 1.8 14 5 8 8.2 2 5Z"/><path d="m2 8 6 3.2L14 8"/>'
    '<path d="m2 11 6 3.2 6-3.2"/>',
    "theme": '<circle cx="8" cy="8" r="5.6"/><path d="M8 2.4v11.2A5.6 5.6 0 0 0 8 2.4Z" '
    'fill="currentColor"/>',
}


def _icon(name: str) -> str:
    return (
        '<svg class="icon" aria-hidden="true" viewBox="0 0 16 16">'
        f'<use href="#i-{name}"></use></svg>'
    )


def _icon_defs() -> str:
    symbols = "".join(
        f'<symbol id="i-{name}" viewBox="0 0 16 16">{body}</symbol>'
        for name, body in _ICONS.items()
    )
    return f'<svg width="0" height="0" style="position:absolute" aria-hidden="true">{symbols}</svg>'


def _esc(value: t.Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _slug(prefix: str, *parts: t.Optional[str]) -> str:
    raw = "|".join("" if p is None else p for p in parts).encode("utf-8")
    digest = hashlib.sha1(raw).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _short(model_name: str) -> str:
    """``"db"."analytics"."orders"`` -> ``db.analytics.orders`` for display."""
    return model_name.replace('"', "")


def _list_names(names: t.Sequence[str]) -> t.Dict[str, str]:
    """Sidebar labels: drop the catalog when every model shares it."""
    parts = [_short(n).split(".") for n in names]
    if len(parts) > 1 and all(len(p) == 3 for p in parts) and len({p[0] for p in parts}) == 1:
        return {n: ".".join(p[1:]) for n, p in zip(names, parts)}
    return {n: _short(n) for n in names}


def _leaf(model_name: str) -> str:
    return _short(model_name).split(".")[-1]


def _incident_anchor(audit: AuditRecord) -> str:
    return _slug("incident", audit.invocation_id, *audit_key(audit))


def _model_anchor(model_name: str) -> str:
    return _slug("model", model_name)


def _args_text(args_json: t.Optional[str]) -> str:
    """``{"columns": "(id)"}`` -> ``columns := (id)``, the way the model file spells it."""
    if not args_json:
        return ""
    try:
        args = json.loads(args_json)
    except ValueError:
        return args_json
    if not isinstance(args, dict):
        return args_json
    return ", ".join(f"{key} := {value}" for key, value in args.items())


def _audit_label(audit: AuditRecord) -> str:
    """Audit name in code, plus its arguments so two uses of one audit can be told apart."""
    label = f"<code>{_esc(audit.audit_name)}</code>"
    args = _args_text(audit.args_json)
    if args:
        label += f' <span class="args">{_esc(args)}</span>'
    return label


def _num(value: t.Optional[float]) -> str:
    return charts.fmt(value)


def _series_anchor(model_name: str, series_metric: str, column: str) -> str:
    return _slug("series", model_name, series_metric, column)


def _metric_label(metric_name: str, column_name: str) -> str:
    label = _METRIC_LABELS.get(metric_name, metric_name)
    return f"{label} ({column_name})" if column_name else label


def _pill(text: str, cls: str = "") -> str:
    return f'<span class="pill {_esc(cls or text)}">{_esc(text)}</span>'


def _status_badge(status: str) -> str:
    known = ("pass", "fail", "skip", "error", "warn")
    return _pill(status, status if status in known else "skip")


def _blocking_label(blocking: t.Optional[bool]) -> str:
    if blocking is True:
        return "blocking"
    if blocking is False:
        return "non-blocking"
    return "unknown"


def _model_status(model: ModelReport) -> str:
    """``fail`` (blocking failure or error), ``warn`` (non-blocking failure or latest-point
    anomaly), or ``pass``; drives the status dot, the filter, and the model pill."""
    if model.failing_blocking_audits:
        return "fail"
    if model.warning_audits or any(s.points and s.points[-1].anomaly for s in model.metrics):
        return "warn"
    return "pass"


_MODEL_STATUS_TEXT = {"fail": "failing", "warn": "warnings", "pass": "healthy"}


def _table(head: str, body: str, cls: str = "") -> str:
    class_attr = f' class="{cls}"' if cls else ""
    return (
        f'<div class="table-wrap"><table{class_attr}><thead><tr>{head}</tr></thead>'
        f"{body}</table></div>"
    )


def _time(value: t.Optional[str]) -> str:
    """``2024-01-03 09:08:56`` -> ``09:08:56``, with the full value in the title."""
    if not value:
        return "<time>-</time>"
    clock = value[11:19] if len(value) >= 19 else value
    return f'<time datetime="{_esc(value)}" title="{_esc(value)} UTC">{_esc(clock)}</time>'


def _duration(ms: t.Optional[int]) -> str:
    if ms is None:
        return "-"
    if ms < 1000:
        return f"{ms} ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f} s"
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# --- Dashboard ---------------------------------------------------------------------------------


def _tiles(data: ReportData) -> str:
    def tile(href: str, label: str, value: int, cls: str, note: str) -> str:
        return (
            f'<a class="tile" href="#{href}"><span class="tile-label">{_esc(label)}</span>'
            f'<span class="tile-value {cls}">{value}</span>'
            f'<span class="tile-note" title="{_esc(note)}">{_esc(note)}</span></a>'
        )

    blocking = sum(len(m.failing_blocking_audits) for m in data.failing_models)
    warnings = sum(len(m.warning_audits) for m in data.warning_models)
    return (
        '<div class="tiles">'
        + tile(
            "overview-failures",
            "Blocking failures",
            blocking,
            "red" if blocking else "green",
            ", ".join(_leaf(m.model_name) for m in data.failing_models) or "none in scope",
        )
        + tile(
            "overview-anomalies",
            "Metric anomalies",
            len(data.anomalies),
            "amber" if data.anomalies else "green",
            ", ".join(sorted({_leaf(a.model_name) for a in data.anomalies}))
            or "none at the latest interval",
        )
        + tile(
            "overview-warnings",
            "Non-blocking failures",
            warnings,
            "",
            ", ".join(_leaf(m.model_name) for m in data.warning_models) or "none in scope",
        )
        + tile(
            "tests",
            "Passing audits",
            data.passing_audit_count,
            "green" if data.passing_audit_count else "",
            f"latest result per interval, {_plural(len(data.models), 'model')}",
        )
        + "</div>"
    )


def _plate(pill: str, title: str, detail: str, href: str, link: str, when: t.Optional[str]) -> str:
    """The one reversed element per screen, for the single most urgent item."""
    return (
        f'<div class="plate">{pill}'
        f'<div><div class="plate-title">{title}</div><div class="plate-detail">{detail} · '
        f'<a href="#{href}">{_esc(link)}</a></div></div>'
        f'<span class="plate-when">{_time(when)}</span></div>'
    )


def _audit_plate(model_name: str, audit: AuditRecord) -> str:
    day = _esc((audit.interval_start or "-")[:10])
    if audit.status == "error":
        pill, detail = _pill("error", "red"), f"audit errored on {day}"
    elif audit.blocking:
        count = "-" if audit.violation_count is None else _plural(audit.violation_count, "row")
        pill, detail = _pill("blocking", "red"), f"{_esc(count)} failed on {day}; not promoted"
    else:
        count = "-" if audit.violation_count is None else _plural(audit.violation_count, "row")
        pill, detail = _pill("non-blocking", "amber"), f"{_esc(count)} failed on {day}"
    title = f"<code>{_esc(_short(model_name))}</code> · {_audit_label(audit)}"
    return _plate(pill, title, detail, _incident_anchor(audit), "Open incident", audit.started_at)


def _anomaly_plate(model_name: str, series: MetricSeries) -> str:
    point = series.points[-1]
    title = (
        f"<code>{_esc(_short(model_name))}</code> · "
        f"{_esc(_metric_label(series.metric_name, series.column_name))}"
    )
    detail = (
        f"{_esc(_num(point.value))} on {_esc(point.interval_start[:10])}, "
        f"expected {_esc(_expected(point.mean, point.stddev, series.threshold))}"
    )
    anchor = _series_anchor(model_name, series.metric_name, series.column_name)
    return _plate(_pill("anomaly", "amber"), title, detail, anchor, "Open chart", None)


def _plate_row(plate: str) -> str:
    return f'<tr class="plate-row"><td colspan="5">{plate}</td></tr>'


def _expected(mean: t.Optional[float], stddev: t.Optional[float], k: float) -> str:
    if mean is None or stddev is None:
        return "-"
    lo, hi = mean - k * stddev, mean + k * stddev
    if abs(hi - lo) < 1e-12:
        return _num(mean)
    return f"{_num(lo)} – {_num(hi)}"


def _group(group_id: str, title: str, rows: t.List[str], count: int, empty: str) -> str:
    body = "".join(rows) or f'<tr><td class="none" colspan="5">{_esc(empty)}</td></tr>'
    return (
        f'<tbody class="group" id="{group_id}"><tr class="group-head">'
        f'<td colspan="5">{_esc(title)} · {count}</td></tr>{body}</tbody>'
    )


def _audit_rows(
    models: t.Sequence[ModelReport], warnings: bool, skip: t.Optional[AuditRecord] = None
) -> t.List[str]:
    rows = []
    for model in models:
        for audit in model.warning_audits if warnings else model.failing_blocking_audits:
            if audit is skip:
                continue
            count = "-" if audit.violation_count is None else _plural(audit.violation_count, "row")
            if warnings:
                label, kind = "non-blocking", "warn"
            elif audit.status == "error":
                label, kind = "error", "error"
            else:
                label, kind = "blocking", "fail"
            owner = f'<div class="args">owner {_esc(model.owner)}</div>' if model.owner else ""
            rows.append(
                "<tr>"
                f'<td><a class="name" href="#{_model_anchor(model.model_name)}"><code>'
                f"{_esc(_short(model.model_name))}</code></a>{owner}</td>"
                f'<td><a class="sig" href="#{_incident_anchor(audit)}">'
                f"{_audit_label(audit)}</a></td>"
                f'<td class="mono">{_esc((audit.interval_start or "-")[:10])}</td>'
                f'<td class="num"><span class="strong">{_esc(count)}</span>'
                '<span class="exp">expected 0</span></td>'
                f"<td>{_pill(label, kind)}</td></tr>"
            )
    return rows


def _series_for(data: ReportData, model_name: str, metric: str, column: str) -> MetricSeries:
    for model in data.models:
        if model.model_name == model_name:
            for series in model.metrics:
                if series.metric_name == metric and series.column_name == column:
                    return series
    raise KeyError((model_name, metric, column))


def _anomaly_rows(data: ReportData, skip_first: bool = False) -> t.List[str]:
    rows = []
    for index, anomaly in enumerate(data.anomalies):
        if skip_first and index == 0:
            continue
        point = anomaly.point
        try:
            series = _series_for(data, anomaly.model_name, anomaly.metric_name, anomaly.column_name)
            threshold = series.threshold
        except KeyError:
            threshold = 3.0
        anchor = _series_anchor(anomaly.model_name, anomaly.metric_name, anomaly.column_name)
        rows.append(
            "<tr>"
            f'<td><a class="name" href="#{_model_anchor(anomaly.model_name)}"><code>'
            f"{_esc(_short(anomaly.model_name))}</code></a></td>"
            f'<td><a class="sig" href="#{anchor}">'
            f"{_esc(_metric_label(anomaly.metric_name, anomaly.column_name))}"
            f'</a> <span class="args"><code>{_esc(anomaly.metric_name)}</code></span></td>'
            f'<td class="mono">{_esc(point.interval_start[:10])}</td>'
            f'<td class="num"><span class="strong">{_esc(_num(point.value))}</span>'
            f'<span class="exp">expected {_esc(_expected(point.mean, point.stddev, threshold))}'
            "</span></td>"
            f"<td>{_pill('anomaly')}</td></tr>"
        )
    return rows


def _open_count(data: ReportData) -> int:
    return (
        sum(len(m.failing_blocking_audits) for m in data.failing_models)
        + len(data.anomalies)
        + sum(len(m.warning_audits) for m in data.warning_models)
    )


def _incident_table(data: ReportData, with_plate: bool, prefix: str = "overview") -> str:
    """Blocking failures, then anomalies, then non-blocking failures. With ``with_plate`` the
    single most urgent item leads its group as the reversed plate instead of an ordinary row."""
    urgent_audit: t.Optional[AuditRecord] = None
    urgent_model: t.Optional[str] = None
    plate_anomaly = False
    if with_plate:
        for model in data.failing_models:
            if model.failing_blocking_audits:
                urgent_audit, urgent_model = model.failing_blocking_audits[0], model.model_name
                break
        plate_anomaly = urgent_audit is None and bool(data.anomalies)

    failures = _audit_rows(data.failing_models, warnings=False, skip=urgent_audit)
    if urgent_audit is not None and urgent_model is not None:
        failures.insert(0, _plate_row(_audit_plate(urgent_model, urgent_audit)))
    anomalies = _anomaly_rows(data, skip_first=plate_anomaly)
    if plate_anomaly:
        first = data.anomalies[0]
        series = _series_for(data, first.model_name, first.metric_name, first.column_name)
        anomalies.insert(0, _plate_row(_anomaly_plate(first.model_name, series)))
    warnings = _audit_rows(data.warning_models, warnings=True)
    return _table(
        '<th>Model</th><th>Signal</th><th>Interval</th><th class="num">Value</th><th>Type</th>',
        _group(
            f"{prefix}-failures",
            "Blocking failures and errors",
            failures,
            sum(len(m.failing_blocking_audits) for m in data.failing_models),
            "No failing blocking audits in scope.",
        )
        + _group(
            f"{prefix}-anomalies",
            "Metric anomalies at the latest interval",
            anomalies,
            len(data.anomalies),
            "No metric anomalies at the latest interval.",
        )
        + _group(
            f"{prefix}-warnings",
            "Non-blocking failures",
            warnings,
            sum(len(m.warning_audits) for m in data.warning_models),
            "No failing non-blocking audits in scope.",
        ),
        cls="incidents-table",
    )


def _render_overview(data: ReportData) -> str:
    open_count = _open_count(data)
    open_cls = "red" if data.failing_models else ("amber" if open_count else "green")
    return (
        '<section id="overview" class="view" data-title="Dashboard" aria-labelledby="overview-h">'
        '<h2 id="overview-h">Dashboard</h2>'
        f"{_tiles(data)}"
        '<div class="panel">'
        f'<div class="panel-head"><h3>Incidents</h3>{_pill(f"{open_count} open", open_cls)}'
        '<span class="aside">blocking first, then anomalies, then warnings</span></div>'
        f"{_incident_table(data, with_plate=True)}"
        '<a class="panel-foot" href="#incidents">View all incidents</a></div>'
        f"{_health_overview(data)}"
        "</section>"
    )


def _health_overview(data: ReportData) -> str:
    """Every model's interval health in one panel: 'since when' at a glance."""
    rows = []
    for model in data.models:
        if not model.health:
            continue
        status = _model_status(model)
        rows.append(
            f'<tr data-model="{_esc(model.model_name)}" data-status="{status}" '
            f'data-owner="{_esc(model.owner or "")}">'
            f'<td><a href="#{_model_anchor(model.model_name)}"><code>'
            f"{_esc(_short(model.model_name))}</code></a></td>"
            f'<td><div class="viz">{charts.health_strip(model.health, compact=True)}</div></td>'
            f'<td class="mono">{_esc(model.health[-1].interval_start[:10])}</td></tr>'
        )
    if not rows:
        return ""
    return (
        '<div class="panel"><div class="panel-head"><h3>Interval health</h3>'
        '<span class="aside">latest run of each interval, oldest to newest</span></div>'
        + _table(
            "<th>Model</th><th>Intervals</th><th>Latest</th>",
            "<tbody>" + "".join(rows) + "</tbody>",
        )
        + '<div class="panel-body" style="padding-top:0">'
        + _health_legend({h.status for m in data.models for h in m.health})
        + "</div></div>"
    )


def _health_legend(statuses: t.Iterable[str]) -> str:
    present = sorted(set(statuses), key=list(charts.STATUS_LABEL).index)
    return (
        '<ul class="legend">'
        + "".join(
            f'<li><span class="swatch s-{status}"></span>{charts.STATUS_LETTER[status]} '
            f"{_esc(charts.STATUS_LABEL[status])}</li>"
            for status in present
        )
        + "</ul>"
    )


# --- Model pages -------------------------------------------------------------------------------


def _render_series(model_name: str, series: MetricSeries) -> str:
    label = _metric_label(series.metric_name, series.column_name)
    rows = []
    for point in reversed(series.points):  # newest first, like the audit history
        if point.error_message:
            status = _pill("error")
        elif point.anomaly:
            status = _pill("anomaly")
        elif point.mean is None:
            status = _pill("training", "skip")
        else:
            status = _pill("normal", "pass")
        rows.append(
            "<tr>"
            f'<td class="mono">{_esc(point.interval_start)}</td>'
            f'<td class="num">{_esc(_num(point.value))}</td>'
            f'<td class="num">{_esc(_num(point.mean))}</td>'
            f'<td class="num">{_esc(_num(point.stddev))}</td>'
            f'<td class="num">{point.training_points}</td>'
            f"<td>{status}</td>"
            "</tr>"
        )
    k = charts.fmt(series.threshold)
    notes = "".join(
        f'<li class="anomaly-note">{_pill("anomaly")}<span>{_esc(p.interval_start[:10])}: '
        f"{_esc(_num(p.value))}; training mean {_esc(_num(p.mean))}, std dev "
        f"{_esc(_num(p.stddev))}</span></li>"
        for p in series.points
        if p.anomaly
    )
    errors = [p for p in series.points if p.error_message]
    if errors:
        notes += (
            f"<li>{_pill('error')}<span>{_plural(len(errors), 'interval')} could not be "
            f"measured: {_esc(errors[-1].error_message)}</span></li>"
        )
    notes_html = f'<ul class="notes">{notes}</ul>' if notes else ""
    anchor = _series_anchor(model_name, series.metric_name, series.column_name)
    return (
        f'<div class="panel series" id="{anchor}">'
        f'<div class="panel-head"><h3>{_esc(label)}</h3>'
        f'<span class="args"><code>{_esc(series.metric_name)}</code></span>'
        f'<span class="aside">{_plural(len(series.points), "interval")}</span></div>'
        f'<div class="panel-body"><div class="viz">{charts.metric_chart(series, label)}</div>'
        '<ul class="legend">'
        '<li><span class="swatch line"></span>value</li>'
        f'<li><span class="swatch band"></span>expected, mean &plusmn; {_esc(k)} std dev</li>'
        '<li><span class="swatch s-warn"></span>anomaly</li></ul>'
        f"{notes_html}</div>"
        "<details><summary>Values</summary>"
        + _table(
            "<th>Interval start</th>"
            '<th class="num">Value</th><th class="num">Training mean</th>'
            '<th class="num">Std dev</th><th class="num">Training points</th><th>Status</th>',
            "<tbody>" + "".join(rows) + "</tbody>",
        )
        + "</details></div>"
    )


def _schema_rows(changes: t.Sequence[SchemaChange], model_name: t.Optional[str] = None) -> str:
    model_cell = (
        f'<td><a href="#{_model_anchor(model_name)}">'
        f"<code>{_esc(_short(model_name))}</code></a></td>"
        if model_name
        else ""
    )
    return "".join(
        "<tr>"
        f'<td class="mono">{_esc(change.recorded_at or "-")}</td>'
        f"{model_cell}"
        f"<td><code>{_esc(change.column_name)}</code></td>"
        f"<td>{_pill(change.change_type)}</td>"
        f'<td class="mono">{_esc(change.old_type or "-")}</td>'
        f'<td class="mono">{_esc(change.new_type or "-")}</td>'
        f'<td class="mono">{_esc(change.to_version or "-")}</td>'
        "</tr>"
        for change in changes
    )


def _render_schema_changes(changes: t.Sequence[SchemaChange]) -> str:
    return (
        '<div class="panel"><div class="panel-head"><h3>Schema changes</h3>'
        f'<span class="aside">{len(changes)}</span></div>'
        + _table(
            "<th>Recorded</th><th>Column</th><th>Change</th><th>Old type</th><th>New type</th>"
            "<th>Version</th>",
            "<tbody>" + _schema_rows(changes) + "</tbody>",
        )
        + "</div>"
    )


def _render_health(model: ModelReport) -> str:
    if not model.health and not model.runs:
        return ""
    spark = charts.duration_sparkline(model.runs)
    return (
        '<div class="panel"><div class="panel-head"><h3>Interval health</h3>'
        f'<span class="aside">{_plural(len(model.health), "interval")} · '
        f"{_plural(len(model.runs), 'run')}</span></div>"
        f'<div class="panel-body"><div class="viz">{charts.health_strip(model.health)}</div>'
        f"{_health_legend(h.status for h in model.health)}"
        + (
            f'<div class="sub-head">Run duration</div><div class="viz">{spark}</div>'
            if spark
            else ""
        )
        + "</div></div>"
    )


def _audit_history(model: ModelReport) -> str:
    rows: t.List[str] = []
    for audit in model.audits:
        count = "-" if audit.violation_count is None else str(audit.violation_count)
        audit_cell = _audit_label(audit)
        if audit.status in ("fail", "error"):
            audit_cell = f'<a href="#{_incident_anchor(audit)}">{audit_cell}</a>'
        rows.append(
            "<tr>"
            f'<td class="mono">{_esc(audit.interval_start or "-")}</td>'
            f"<td>{audit_cell}</td>"
            f"<td>{_status_badge(audit.status)}</td>"
            f"<td>{_esc(_blocking_label(audit.blocking))}</td>"
            f'<td class="num">{_esc(count)}</td>'
            f"<td>{_esc(audit.environment or '-')}</td>"
            "</tr>"
        )
    if not rows:
        return '<div class="panel"><p class="empty">No audits in scope.</p></div>'
    table = _table(
        "<th>Interval start</th><th>Audit</th><th>Status</th><th>Mode</th>"
        '<th class="num">Violations</th><th>Run env</th>',
        "<tbody>" + "".join(rows) + "</tbody>",
    )
    open_attr = " open" if len(rows) <= 12 else ""
    return (
        f'<div class="panel"><details class="audits"{open_attr}><summary>Audit history, '
        f"{_plural(len(rows), 'result')}</summary>{table}</details></div>"
    )


def _model_plate(model: ModelReport) -> str:
    """This page's one reversed plate: a failing audit, else the latest-point anomaly."""
    audits = list(model.failing_blocking_audits) + list(model.warning_audits)
    if audits:
        return _audit_plate(model.model_name, audits[0])
    for series in model.metrics:
        if series.points and series.points[-1].anomaly:
            return _anomaly_plate(model.model_name, series)
    return ""


def _render_model_card(model: ModelReport) -> str:
    anchor = _model_anchor(model.model_name)
    status = _model_status(model)
    version = next((r.version for r in reversed(model.runs) if r.version), None)
    facts = []
    if model.owner:
        facts.append(f"Owner <strong>{_esc(model.owner)}</strong>")
    if version:
        facts.append(f'Version <strong class="mono">{_esc(version)}</strong>')
    if model.health:
        latest = _esc(model.health[-1].interval_start[:10])
        facts.append(f'Latest interval <strong class="mono">{latest}</strong>')
    facts_html = (
        f'<div class="facts">{"".join(f"<span>{f}</span>" for f in facts)}</div>' if facts else ""
    )
    cols_html = (
        '<div class="cols">'
        + "".join(
            f'<span class="col"><b>{_esc(col)}</b> {_esc(kind)}</span>'
            for col, kind in model.columns
        )
        + "</div>"
        if model.columns
        else ""
    )
    series = "".join(_render_series(model.model_name, s) for s in model.metrics)
    metrics_html = f'<div class="grid-2">{series}</div>' if series else ""
    changes_html = _render_schema_changes(model.schema_changes) if model.schema_changes else ""
    return (
        f'<article id="{anchor}" class="model" data-model="{_esc(model.model_name)}" '
        f'data-status="{status}" data-owner="{_esc(model.owner or "")}" '
        f'data-title="{_esc(_short(model.model_name))}">'
        '<div class="model-head">'
        f'<h3 title="{_esc(model.model_name)}">{_esc(_short(model.model_name))}</h3>'
        f"{_pill(_MODEL_STATUS_TEXT[status], status)}</div>"
        f"{_model_plate(model)}{facts_html}{cols_html}{_render_health(model)}{metrics_html}{changes_html}"
        f"{_audit_history(model)}"
        "</article>"
    )


def _models_index(data: ReportData) -> str:
    rows = []
    for model in data.models:
        status = _model_status(model)
        anomalies = sum(1 for s in model.metrics if s.points and s.points[-1].anomaly)
        failing = len(model.failing_blocking_audits) + len(model.warning_audits)
        latest = _esc(model.health[-1].interval_start[:10]) if model.health else "-"
        rows.append(
            f'<tr data-model="{_esc(model.model_name)}" data-status="{status}" '
            f'data-owner="{_esc(model.owner or "")}">'
            f'<td><a href="#{_model_anchor(model.model_name)}"><code>'
            f"{_esc(_short(model.model_name))}</code></a></td>"
            f"<td>{_pill(_MODEL_STATUS_TEXT[status], status)}</td>"
            f"<td>{_esc(model.owner or '-')}</td>"
            f'<td class="num">{len(model.health)}</td>'
            f'<td class="num">{failing}</td><td class="num">{anomalies}</td>'
            f'<td class="mono">{latest}</td></tr>'
        )
    return (
        '<div class="panel models-index"><div class="panel-head"><h3>All models</h3>'
        f'<span class="aside">{len(data.models)} in scope</span></div>'
        + _table(
            "<th>Model</th><th>Status</th><th>Owner</th>"
            '<th class="num">Intervals</th><th class="num">Failing audits</th>'
            '<th class="num">Anomalies</th><th>Latest</th>',
            "<tbody>" + "".join(rows) + "</tbody>",
        )
        + "</div>"
    )


def _render_models(data: ReportData) -> str:
    if not data.models:
        body = '<div class="panel"><p class="empty">No models in scope.</p></div>'
    else:
        body = _models_index(data) + "".join(_render_model_card(model) for model in data.models)
    return (
        '<section id="models" class="view" data-title="Models" aria-labelledby="models-h">'
        f'<h2 id="models-h">Models</h2>{body}</section>'
    )


# --- Incidents, test results, runs, schema -----------------------------------------------------


def _sample_table(samples: t.Sequence[str]) -> str:
    """Sample rows as a table: one column per JSON key, in first-seen order."""
    records: t.List[t.Dict[str, t.Any]] = []
    keys: t.List[str] = []
    for raw in samples:
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        record = value if isinstance(value, dict) else {"value": value}
        records.append(record)
        for key in record:
            if key not in keys:
                keys.append(key)

    def cell(value: t.Any) -> str:
        if value is None:
            return '<td class="mono muted">null</td>'
        if isinstance(value, (dict, list)):
            value = json.dumps(value, sort_keys=True)
        return f'<td class="mono">{_esc(value)}</td>'

    head = "".join(f"<th>{_esc(key)}</th>" for key in keys)
    body = "".join(
        "<tr>" + "".join(cell(record.get(key)) for key in keys) + "</tr>" for record in records
    )
    raw_json = "\n".join(_esc(row) for row in samples)
    return (
        f'<p class="samples-head">Sample rows · {len(samples)}</p>'
        f'<div class="table-wrap"><table class="samples"><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
        f"<details><summary>Raw JSON</summary><pre>{raw_json}</pre></details>"
    )


def _render_incident_card(incident: AuditRecord) -> str:
    anchor = _incident_anchor(incident)
    count = "-" if incident.violation_count is None else str(incident.violation_count)
    interval_str = _esc(incident.interval_start or "-")
    if incident.interval_end:
        interval_str = f"{interval_str} to {_esc(incident.interval_end)}"
    if incident.status == "error":
        head_pill = _pill("error")
    elif incident.blocking:
        head_pill = _pill("blocking", "fail")
    else:
        head_pill = _pill("non-blocking", "warn")
    model_link = (
        f'<a href="#{_model_anchor(incident.model_name)}">'
        f"<code>{_esc(incident.model_name)}</code></a>"
    )
    details_items: t.List[t.Tuple[str, str]] = [
        ("Model", model_link),
        ("Status", f"{_status_badge(incident.status)} {_esc(_blocking_label(incident.blocking))}"),
        ("Arguments", f"<code>{_esc(_args_text(incident.args_json) or '-')}</code>"),
        ("Violations", f'<span class="mono">{_esc(count)}</span>'),
        ("Interval", f'<span class="mono">{interval_str}</span>'),
        ("Snapshot ID", f"<code>{_esc(incident.snapshot_id or '-')}</code>"),
        ("Version", f"<code>{_esc(incident.version or '-')}</code>"),
        ("Environment", _esc(incident.environment or "-")),
        ("Invocation", f"<code>{_esc(incident.invocation_id)}</code>"),
    ]
    if incident.error_message:
        details_items.append(("Error", f"<code>{_esc(incident.error_message)}</code>"))
    dl_html = (
        '<dl class="kv">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in details_items) + "</dl>"
    )
    sql_html = (
        '<details class="sql"><summary>Rendered SQL</summary>'
        f"<pre>{_esc(incident.query_sql)}</pre></details>"
        if incident.query_sql
        else ""
    )
    samples_html = _sample_table(incident.samples) if incident.samples else ""
    return (
        f'<article id="{anchor}" class="panel incident">'
        f'<div class="panel-head">{head_pill}'
        f"<h3>{_audit_label(incident)} on <code>{_esc(_short(incident.model_name))}</code></h3>"
        f'<span class="aside mono">{_esc((incident.interval_start or "-")[:10])}</span></div>'
        f"{dl_html}{sql_html}{samples_html}</article>"
    )


def _render_incidents(data: ReportData) -> str:
    open_count = _open_count(data)
    cards = "".join(_render_incident_card(incident) for incident in data.incidents)
    count_cls = "red" if data.failing_models else ("amber" if open_count else "green")
    return (
        '<section id="incidents" class="view" data-title="Incidents" aria-labelledby="incidents-h">'
        f'<h2 id="incidents-h">Incidents {_pill(str(open_count), count_cls)}</h2>'
        '<div class="panel"><div class="panel-head"><h3>All open</h3>'
        f"{_pill(f'{open_count} open', count_cls)}"
        '<span class="aside">failed audits are detailed below; anomalies link to their chart</span>'
        f"</div>{_incident_table(data, with_plate=False, prefix='incidents')}</div>"
        + (cards or '<div class="panel"><p class="empty">No failed audits in scope.</p></div>')
        + "</section>"
    )


def _render_tests(data: ReportData) -> str:
    audits = sorted(
        (a for m in data.models for a in m.audits),
        key=lambda a: (a.interval_start or "", a.started_at or "", a.model_name),
        reverse=True,
    )
    rows = []
    for audit in audits:
        count = "-" if audit.violation_count is None else str(audit.violation_count)
        name = _audit_label(audit)
        if audit.status in ("fail", "error"):
            name = f'<a href="#{_incident_anchor(audit)}">{name}</a>'
        rows.append(
            "<tr>"
            f"<td>{_status_badge(audit.status)}</td>"
            f'<td><a href="#{_model_anchor(audit.model_name)}"><code>'
            f"{_esc(_short(audit.model_name))}</code></a></td>"
            f"<td>{name}</td>"
            f'<td class="mono">{_esc((audit.interval_start or "-")[:10])}</td>'
            f'<td class="num">{_esc(count)}</td>'
            f"<td>{_esc(_blocking_label(audit.blocking))}</td>"
            f"<td>{_esc(audit.environment or '-')}</td></tr>"
        )
    body = (
        _table(
            "<th>Status</th><th>Model</th><th>Audit</th><th>Interval</th>"
            '<th class="num">Violations</th><th>Mode</th><th>Run env</th>',
            "<tbody>" + "".join(rows) + "</tbody>",
        )
        if rows
        else '<p class="empty">No audit results in scope.</p>'
    )
    return (
        '<section id="tests" class="view" data-title="Test results" aria-labelledby="tests-h">'
        f'<h2 id="tests-h">Test results {_pill(str(len(rows)), "skip")}</h2>'
        f'<div class="panel">{body}</div></section>'
    )


def _run_summary(inv: InvocationRecord) -> str:
    parts = []
    if inv.model_runs:
        parts.append(_plural(inv.model_runs, "interval"))
    if inv.failing_audits:
        parts.append(_plural(inv.failing_audits, "failing audit"))
    elif inv.audits:
        parts.append(f"{inv.audits} audits pass")
    if inv.failed_runs and not inv.failing_audits:
        parts.append(f"{inv.failed_runs} failed")
    return " · ".join(parts) or "nothing evaluated in scope"


def _run_anchor(inv: InvocationRecord) -> str:
    return _slug("run", inv.invocation_id)


def _render_runs(data: ReportData) -> str:
    rows = "".join(
        f'<tr id="{_run_anchor(inv)}">'
        f"<td>{_pill(inv.status or 'unknown', inv.status or 'skip')}</td>"
        f"<td><strong>{_esc(inv.command or '?')}</strong></td>"
        f"<td>{_esc(inv.environment or '-')}</td>"
        f'<td class="mono">{_esc(inv.started_at or "-")}</td>'
        f'<td class="num">{_esc(_duration(inv.duration_ms))}</td>'
        f'<td class="num">{inv.model_runs}'
        f"{f' ({inv.failed_runs} failed)' if inv.failed_runs else ''}</td>"
        f'<td class="num">{inv.audits}'
        f"{f' ({inv.failing_audits} failing)' if inv.failing_audits else ''}</td>"
        f"<td><code>{_esc(inv.invocation_id[:12])}</code></td></tr>"
        for inv in data.invocations
    )
    body = (
        _table(
            "<th>Status</th><th>Command</th><th>Environment</th><th>Started (UTC)</th>"
            '<th class="num">Duration</th><th class="num">Model runs</th>'
            '<th class="num">Audits</th><th>Invocation</th>',
            f"<tbody>{rows}</tbody>",
        )
        if rows
        else '<p class="empty">No plans or runs recorded for this environment.</p>'
    )
    return (
        '<section id="runs" class="view" data-title="Model runs" aria-labelledby="runs-h">'
        f'<h2 id="runs-h">Model runs {_pill(str(len(data.invocations)), "skip")}</h2>'
        f'<div class="panel">{body}</div></section>'
    )


def _render_schema(data: ReportData) -> str:
    rows = "".join(_schema_rows(m.schema_changes, m.model_name) for m in data.models)
    total = sum(len(m.schema_changes) for m in data.models)
    body = (
        _table(
            "<th>Recorded</th><th>Model</th><th>Column</th><th>Change</th><th>Old type</th>"
            "<th>New type</th><th>Version</th>",
            f"<tbody>{rows}</tbody>",
        )
        if rows
        else '<p class="empty">No schema changes recorded. Turn them on with '
        '<code>monitors={"schema": True}</code>.</p>'
    )
    return (
        '<section id="schema" class="view" data-title="Schema changes" aria-labelledby="schema-h">'
        f'<h2 id="schema-h">Schema changes {_pill(str(total), "skip")}</h2>'
        f'<div class="panel">{body}</div></section>'
    )


def _render_state_warning(data: ReportData) -> str:
    if data.state_available:
        return ""
    return (
        f'<div id="state-warning" class="banner" role="alert">{_icon("incident")}<div>'
        f"SQLMesh state for environment <code>{_esc(data.environment)}</code> could not be read, "
        "or the environment does not exist yet. This report shows only the latest invocation "
        "that targeted it, not everything currently deployed there.</div></div>"
    )


# --- Shell -------------------------------------------------------------------------------------


def _render_sidebar(data: ReportData) -> str:
    owners = sorted({m.owner for m in data.models if m.owner})
    owner_options = "".join(f'<option value="{_esc(o)}">{_esc(o)}</option>' for o in owners)
    schema_total = sum(len(m.schema_changes) for m in data.models)
    test_total = sum(len(m.audits) for m in data.models)

    def count(n: int, cls: str = "") -> str:
        return f'<span class="count {cls}">{n}</span>'

    def view(target: str, icon: str, label: str, badge: str = "") -> str:
        return (
            f'<li><a href="#{target}" data-nav="{target}">{_icon(icon)}<span>{_esc(label)}</span>'
            f"{badge}</a></li>"
        )

    labels = _list_names([m.model_name for m in data.models])
    severity = {"fail": 0, "warn": 1, "pass": 2}
    ordered = sorted(data.models, key=lambda m: (severity[_model_status(m)], labels[m.model_name]))
    items = "".join(
        f'<li data-model="{_esc(m.model_name)}" data-short="{_esc(labels[m.model_name])}" '
        f'data-status="{_model_status(m)}" data-owner="{_esc(m.owner or "")}">'
        f'<a href="#{_model_anchor(m.model_name)}" data-nav="{_model_anchor(m.model_name)}" '
        f'title="{_esc(m.model_name)}: {_MODEL_STATUS_TEXT[_model_status(m)]}">'
        f'<span class="dot {_model_status(m)}" aria-hidden="true"></span>'
        f'<span class="name">{_esc(labels[m.model_name])}</span></a></li>'
        for m in ordered
    )
    guide = (
        f"{labels[ordered[0].model_name]} → {labels[ordered[-1].model_name]}"
        if data.models
        else "no models in scope"
    )
    incidents = _open_count(data)
    meta = f"{_esc(data.environment)} · {_esc(data.schema)} · {_esc(data.gateway)}"
    return (
        '<nav class="sidebar" aria-label="Report">'
        '<div class="brand"><span class="brand-name">sqlmesh-observe</span>'
        f'<span class="brand-meta">{meta}</span></div>'
        '<ul class="views">'
        + view("overview", "dashboard", "Dashboard")
        + view(
            "incidents",
            "incident",
            "Incidents",
            count(incidents, "red" if data.failing_models else ("amber" if incidents else "")),
        )
        + view("tests", "tests", "Test results", count(test_total))
        + view("runs", "runs", "Model runs", count(len(data.invocations)))
        + view(
            "schema",
            "schema",
            "Schema changes",
            count(schema_total, "amber" if schema_total else ""),
        )
        + view("models", "models", "Models", count(len(data.models)))
        + "</ul>"
        '<div class="side-head">Models '
        f'<span class="guide" id="model-guide">{_esc(guide)}</span></div>'
        '<div class="filters">'
        '<label>Search<input type="search" id="model-search" placeholder="Filter by name" '
        'autocomplete="off"></label>'
        '<label>Status<select id="status-filter"><option value="">All</option>'
        '<option value="fail">Failing</option><option value="warn">Warnings</option>'
        '<option value="pass">Healthy</option></select></label>'
        '<label>Owner<select id="owner-filter"><option value="">All</option>'
        f"{owner_options}</select></label></div>"
        '<p class="filter-note" id="filter-note" aria-live="polite"></p>'
        f'<ul class="model-list">{items}</ul>'
        "</nav>"
    )


def _run_outcome(inv: InvocationRecord) -> t.Tuple[str, str]:
    """(state class, word) for a run's dot: red only for blocking failures, errors, or a failed
    command; amber for non-blocking failures; green when clean."""
    if inv.blocking_failures or inv.status == "failure" or inv.failed_runs:
        return "fail", inv.status if inv.status == "failure" else "blocking failure"
    if inv.failing_audits:
        return "warn", "warnings"
    if inv.status == "unfinished":
        return "warn", "unfinished"
    return "pass", ""


def _render_rail(data: ReportData) -> str:
    status_box = (
        '<div class="status-box"><div class="line1"><span class="dot warn"></span>'
        'State unavailable</div><div class="muted">Showing only the latest invocation that '
        "targeted this environment.</div></div>"
        if not data.state_available
        else ""
    )
    items = []
    for inv in data.invocations[:15]:
        state, word = _run_outcome(inv)
        word_html = f' <span class="st {state}">{_esc(word)}</span>' if word else ""
        items.append(
            f'<li><a href="#{_run_anchor(inv)}">'
            f'<span class="dot {state}" aria-hidden="true"></span>'
            f'<span><span class="what">{_esc(inv.command or "?")} · '
            f"{_esc(inv.environment or '-')}</span>{word_html}"
            f'<span class="detail">{_esc(_run_summary(inv))}</span></span>'
            f"{_time(inv.started_at)}</a></li>"
        )
    runs = (
        f'<ul class="runs">{"".join(items)}</ul><a class="rail-foot" href="#runs">All runs</a>'
        if items
        else '<p class="muted">No plans or runs recorded.</p>'
    )
    return f'<aside class="rail" aria-label="Run log">{status_box}<h2>Run log</h2>{runs}</aside>'


def render_report(data: ReportData, start: t.Optional[str] = None) -> str:
    """Render ``data`` as a single self-contained HTML5 document.

    ``start`` is the id of the element to open on when the URL has no hash (the ``serve`` routes
    for a model or an incident). The static report leaves it unset.
    """
    title = f"sqlmesh-observe · {data.environment}"
    start_attr = f' data-start="{_esc(start)}"' if start else ""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en" class="no-js">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '  <meta name="color-scheme" content="dark light">\n'
        f"  <title>{_esc(title)}</title>\n"
        f"  <style>{_CSS}</style>\n"
        "</head>\n"
        f'<body data-env="{_esc(title)}"{start_attr}>\n'
        f"{_icon_defs()}\n"
        '<div class="app">\n'
        f"{_render_sidebar(data)}\n"
        '<div class="work">\n'
        '<header class="topbar">'
        '<h1 id="view-title">Dashboard</h1>'
        f'<span class="chip">Environment <code>{_esc(data.environment)}</code></span>'
        f'<span class="chip spacer">Generated <code>{_esc(data.generated_at)}</code></span>'
        f'<button type="button" class="btn js-only" id="theme-toggle">{_icon("theme")}'
        '<span class="label">Theme</span></button>'
        "</header>\n"
        "<main>\n"
        f"{_render_state_warning(data)}\n"
        f"{_render_overview(data)}\n"
        f"{_render_incidents(data)}\n"
        f"{_render_models(data)}\n"
        f"{_render_tests(data)}\n"
        f"{_render_runs(data)}\n"
        f"{_render_schema(data)}\n"
        "</main>\n"
        "</div>\n"
        f"{_render_rail(data)}\n"
        "</div>\n"
        f'<script id="report-filters">{_SCRIPT}</script>\n'
        "</body>\n"
        "</html>\n"
    )


def write_report(data: ReportData, output: t.Union[str, Path]) -> Path:
    """Render ``data`` and write it to ``output``, creating parent directories if needed."""
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_report(data), encoding="utf-8")
    return destination
