/* sqlmesh-observe report renderer.
 *
 * Reads the JSON payload in #report-data (see payload.py for the schema) and draws every view in
 * the browser. Status, scope, and anomaly flags come from Python; this file only displays,
 * filters, and sorts. When the payload is missing or its schema_version is unknown, the page
 * keeps the HTML fallback the server wrote.
 */
(function () {
  "use strict";

  var SCHEMA_VERSION = 1;
  var RH = 36; // list row height, px; matches --row in report.css
  var THEME_KEY = "sqlmesh-observe-theme";
  var TREE_KEY = "sqlmesh-observe-tree";

  var source = document.getElementById("report-data");
  var D;
  try {
    D = JSON.parse(source ? source.textContent : "null");
  } catch (e) {
    return;
  }
  if (!D || D.schema_version !== SCHEMA_VERSION) return;

  // ---------------------------------------------------------------- helpers

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function num(v) {
    if (v == null || v !== v) return "-";
    if (Number.isInteger(v) && Math.abs(v) < 1e15) return v.toLocaleString("en-US");
    if (Math.abs(v) < 1) return v.toFixed(3);
    return v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function compact(v) {
    var a = Math.abs(v);
    if (a >= 1e9) return +(v / 1e9).toFixed(1) + "B";
    if (a >= 1e6) return +(v / 1e6).toFixed(1) + "M";
    if (a >= 1e4) return +(v / 1e3).toFixed(1) + "k";
    if (a > 0 && a < 1) return +v.toPrecision(2) + "";
    return +v.toFixed(2) + "";
  }
  function duration(ms) {
    if (ms == null) return "-";
    if (ms < 1000) return ms + " ms";
    var s = ms / 1000;
    if (s < 60) return s.toFixed(1) + " s";
    return Math.floor(s / 60) + "m " + String(Math.floor(s % 60)).padStart(2, "0") + "s";
  }
  function plural(n, word) { return num(n) + " " + word + (n === 1 ? "" : "s"); }
  function undelta(a) {
    if (a.length === 2 && a[0] < 0) {
      var r = [];
      for (var i = 0; i < -a[0]; i++) r.push(a[1] + i);
      return r;
    }
    var out = [];
    for (var j = 0; j < a.length; j++) out.push(j ? out[j - 1] + a[j] : a[j]);
    return out;
  }
  function day(i) { return i == null || i < 0 ? "-" : D.dates[i]; }
  function time(s) { return Date.parse(String(s).replace(" ", "T") + (String(s).length <= 10 ? "T00:00:00Z" : "Z")); }
  function shortDate(s) {
    var t = new Date(time(s));
    if (isNaN(t)) return String(s);
    return t.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
  }
  function clock(s) { return s && s.length >= 16 ? s.slice(11, 16) : (s || "-"); }
  function storeGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function storeSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } }
  function h(tag, attrs, html) {
    var e = document.createElement(tag);
    for (var k in attrs || {}) if (attrs[k] != null) e.setAttribute(k, attrs[k]);
    if (html != null) e.innerHTML = html;
    return e;
  }
  function svg(paths, cls) {
    return '<svg viewBox="0 0 16 16" aria-hidden="true"' + (cls ? ' class="' + cls + '"' : "") +
      ' fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' + paths + "</svg>";
  }
  var ICON = {
    chev: svg('<path d="M6 4l4 4-4 4"/>', "chev"),
    search: svg('<circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14"/>'),
    theme: svg('<circle cx="8" cy="8" r="5.5"/><path d="M8 2.5v11a5.5 5.5 0 0 0 0-11z" fill="currentColor"/>'),
    tree: svg('<path d="M2.5 4h11M2.5 8h11M2.5 12h11"/>'),
    table: svg('<rect x="2.5" y="3" width="11" height="10" rx="1.5"/><path d="M2.5 6.5h11M6.5 6.5V13"/>'),
    chart: svg('<path d="M2.5 12.5l3.5-4 3 2.5 4.5-6"/>')
  };
  // The package mark: three intervals inside the expected band, one breaking out of it.
  var MARK =
    '<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="0.5" y="9" width="19" height="9" rx="2.5" fill="var(--band)"/>' +
    '<rect x="2" y="11.5" width="3" height="5" rx="1" fill="var(--series)"/>' +
    '<rect x="6.25" y="11.5" width="3" height="5" rx="1" fill="var(--series)"/>' +
    '<rect x="10.5" y="11.5" width="3" height="5" rx="1" fill="var(--series)"/>' +
    '<rect x="14.75" y="2" width="3" height="14.5" rx="1" fill="var(--amber-mark)"/></svg>';

  // ---------------------------------------------------------------- store

  var catalogs = {};
  var models = D.models.map(function (m, i) {
    var s = m.n.replace(/"/g, "");
    var parts = s.split(".");
    catalogs[parts.length === 3 ? parts[0] : ""] = 1;
    return {
      i: i, name: m.n, short: s, parts: parts, owner: m.o >= 0 ? D.owners[m.o] : null,
      st: m.st, raw: m
    };
  });
  var dropCatalog = Object.keys(catalogs).length === 1 && !catalogs[""];
  models.forEach(function (m) {
    var p = dropCatalog ? m.parts.slice(1) : m.parts;
    m.display = p.join(".");
    m.group = p.length > 1 ? p.slice(0, -1).join(".") : "(no schema)";
    m.leaf = p[p.length - 1];
    var uses = {}, fails = 0, last = -1;
    m.raw.a.forEach(function (row) {
      row.latest = !uses[row[1]];
      if (row.latest) { uses[row[1]] = 1; if (row[2] === 1 || row[2] === 2) fails++; }
      if (row[0] > last) last = row[0];
    });
    var hd = undelta(m.raw.h[0]);
    if (hd.length && hd[hd.length - 1] > last) last = hd[hd.length - 1];
    m.series = m.raw.m.map(function (s) {
      var dates = undelta(s[3]);
      if (dates.length && dates[dates.length - 1] > last) last = dates[dates.length - 1];
      return {
        metric: D.metrics[s[0]], column: s[1], k: s[2], dates: dates, values: s[4],
        means: s[5], sds: s[6], flags: s[7]
      };
    });
    m.fails = fails;
    m.anoms = m.series.filter(function (s) { return s.flags.length && s.flags[s.flags.length - 1] & 1; }).length;
    m.last = last;
    m.healthDates = hd;
  });
  var RANK = { fail: 0, warn: 1, pass: 2 };
  var STATUS_WORD = { fail: "failing", warn: "warning", pass: "healthy" };
  var owners = D.owners.slice().sort();
  var groupsBySchema = {};
  models.forEach(function (m) { groupsBySchema[m.group] = 1; });
  var schemas = Object.keys(groupsBySchema).sort();

  function familyOf(metric) {
    return metric === "row_count" ? "volume" : metric === "freshness_lag_seconds" ? "freshness" : "columns";
  }
  function seriesStatus(s) {
    var f = s.flags[s.flags.length - 1] || 0;
    return f & 2 ? "fail" : f & 1 ? "warn" : "pass";
  }
  function signalStatus(m, sig) {
    if (sig === "schema") {
      var kinds = m.raw.x.map(function (x) { return x[1]; });
      if (!kinds.length) return "pass";
      return kinds.every(function (k) { return k === "added"; }) ? "warn" : "fail";
    }
    var worst = null;
    m.series.forEach(function (s) {
      if (familyOf(s.metric) !== sig) return;
      var st = seriesStatus(s);
      if (worst == null || RANK[st] < RANK[worst]) worst = st;
    });
    return worst;
  }

  var METRIC_LABEL = {
    row_count: "Row count", freshness_lag_seconds: "Freshness lag (s)", null_rate: "Null rate", mean: "Mean"
  };
  function metricLabel(metric, column) {
    var l = METRIC_LABEL[metric] || metric;
    return column ? l + " (" + column + ")" : l;
  }
  function useLabel(u) {
    var use = D.audits[u] || ["?", ""];
    return "<code>" + esc(use[0]) + "</code>" + (use[1] ? ' <span class="args">' + esc(use[1]) + "</span>" : "");
  }
  var AUDIT_STATUS = ["pass", "fail", "error", "skip"];
  function auditKind(status, blocking) {
    if (status === 2) return "error";
    if (status === 1) return blocking ? "fail" : "warn";
    return status === 3 ? "skip" : "pass";
  }
  var KIND_WORD = { fail: "blocking", warn: "non-blocking", error: "error", pass: "pass", skip: "skipped" };
  function kindPill(kind) {
    var cls = kind === "skip" ? "" : kind;
    return '<span class="pill ' + cls + '">' + esc(KIND_WORD[kind] || kind) + "</span>";
  }
  function statusPill(st) { return '<span class="pill ' + st + '">' + STATUS_WORD[st] + "</span>"; }

  var testRows = null;
  function allTests() {
    if (testRows) return testRows;
    testRows = [];
    models.forEach(function (m) {
      m.raw.a.forEach(function (row) { testRows.push({ m: m, r: row }); });
    });
    return testRows;
  }
  function seriesFor(m, useIndex) {
    var use = D.audits[useIndex];
    if (!use) return null;
    var name = use[0], wanted = null, column = "";
    if (/^volume_anomaly/.test(name)) wanted = "row_count";
    else if (/^freshness_anomaly/.test(name)) wanted = "freshness_lag_seconds";
    else if (/^column_anomaly/.test(name)) {
      var mm = /column\s*:=\s*"?([\w$]+)/.exec(use[1] || "");
      column = mm ? mm[1] : "";
      var metric = /metric\s*:=\s*'?"?([\w]+)/.exec(use[1] || "");
      return m.series.find(function (s) {
        return s.column === column && (!metric || s.metric === metric[1]);
      }) || null;
    }
    if (!wanted) return null;
    return m.series.find(function (s) { return s.metric === wanted; }) || null;
  }

  // ---------------------------------------------------------------- charts

  function niceTicks(lo, hi, count) {
    if (lo === hi) { var pad = Math.abs(lo) * 0.1 || 1; lo -= pad; hi += pad; }
    var step = Math.pow(10, Math.floor(Math.log10((hi - lo) / count)));
    var err = (hi - lo) / count / step;
    step *= err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1;
    var start = Math.floor(lo / step) * step, end = Math.ceil(hi / step) * step, ticks = [];
    for (var v = start; v <= end + step / 2; v += step) ticks.push(+v.toPrecision(12));
    return ticks;
  }

  function frame(host, o) {
    // Shared axes for line and bar charts. o: {times, ys, height, zero}
    var W = Math.max(host.clientWidth || 0, 260), H = o.height || 180;
    var lo = Math.min.apply(null, o.ys), hi = Math.max.apply(null, o.ys);
    if (o.zero) lo = Math.min(0, lo);
    var ticks = niceTicks(lo, hi, 4);
    var left = 12 + 7 * Math.max.apply(null, ticks.map(function (t) { return compact(t).length; }));
    var M = { l: left, r: 10, t: 10, b: 24 };
    var t0 = Math.min.apply(null, o.times), t1 = Math.max.apply(null, o.times);
    var span = t1 - t0, padT = o.padTime || 0;
    function x(t) { return M.l + (span + 2 * padT ? (t - t0 + padT) / (span + 2 * padT) : 0.5) * (W - M.l - M.r); }
    var y0 = ticks[0], y1 = ticks[ticks.length - 1];
    function y(v) { return M.t + (1 - (v - y0) / (y1 - y0 || 1)) * (H - M.t - M.b); }
    var g = "";
    ticks.forEach(function (t) {
      g += '<line class="grid-line" x1="' + M.l + '" x2="' + (W - M.r) + '" y1="' + y(t) + '" y2="' + y(t) + '"/>' +
        '<text class="tick" x="' + (M.l - 8) + '" y="' + (y(t) + 4) + '" text-anchor="end">' + esc(compact(t)) + "</text>";
    });
    var labels = Math.max(2, Math.min(6, Math.floor((W - M.l) / 90)));
    var uniq = o.labelTimes || o.times;
    var stride = Math.max(1, Math.ceil(uniq.length / labels));
    for (var i = uniq.length - 1; i >= 0; i -= stride) {
      g += '<text class="tick" x="' + x(uniq[i]) + '" y="' + (H - 6) + '" text-anchor="middle">' +
        esc(shortDate(new Date(uniq[i]).toISOString().slice(0, 10))) + "</text>";
    }
    g += '<line class="axis-line" x1="' + M.l + '" x2="' + (W - M.r) + '" y1="' + y(y0) + '" y2="' + y(y0) + '"/>';
    return { W: W, H: H, M: M, x: x, y: y, axes: g, y0: y0 };
  }

  function attachHover(host, f, points, tipHtml) {
    // points: [{x, y?}] in chart coordinates; tipHtml(i) -> html
    var svgEl = host.querySelector("svg");
    var cross = svgEl.querySelector(".cross");
    var tip = h("div", { class: "tip", hidden: "" });
    host.appendChild(tip);
    function at(evt) {
      var r = svgEl.getBoundingClientRect();
      var px = (evt.clientX - r.left) * (f.W / r.width);
      var best = -1, dist = Infinity;
      points.forEach(function (p, i) { var d = Math.abs(p.x - px); if (d < dist) { dist = d; best = i; } });
      return best;
    }
    function show(i) {
      if (i < 0) return hide();
      var p = points[i];
      cross.setAttribute("x1", p.x); cross.setAttribute("x2", p.x);
      cross.removeAttribute("hidden");
      tip.innerHTML = tipHtml(i);
      tip.hidden = false;
      var r = svgEl.getBoundingClientRect(), scale = r.width / f.W;
      var left = p.x * scale + 12;
      if (left + tip.offsetWidth > host.clientWidth) left = p.x * scale - tip.offsetWidth - 12;
      tip.style.left = Math.max(0, left) + "px";
      tip.style.top = Math.max(0, (p.y == null ? f.M.t : p.y * scale) - 20) + "px";
    }
    function hide() { tip.hidden = true; cross.setAttribute("hidden", ""); }
    svgEl.addEventListener("mousemove", function (e) { show(at(e)); });
    svgEl.addEventListener("mouseleave", hide);
    svgEl.addEventListener("focus", function () { show(points.length - 1); });
    svgEl.addEventListener("blur", hide);
    svgEl.addEventListener("keydown", function (e) {
      var cur = +(svgEl.dataset.cur || points.length - 1);
      if (e.key === "ArrowLeft") cur = Math.max(0, cur - 1);
      else if (e.key === "ArrowRight") cur = Math.min(points.length - 1, cur + 1);
      else return;
      e.preventDefault();
      svgEl.dataset.cur = cur;
      show(cur);
    });
  }

  function lineChart(host, s, o) {
    // s: {dates, values, means?, sds?, flags?, k?}; o: {label, unit}
    o = o || {};
    var n = s.values.length;
    var times = s.dates.map(function (d) { return time(D.dates[d]); });
    var lo = [], hi = [];
    for (var i = 0; i < n; i++) {
      var mean = s.means ? s.means[i] : null, sd = s.sds ? s.sds[i] : null;
      if (mean != null && sd != null) { lo.push(mean - s.k * sd); hi.push(mean + s.k * sd); }
      else { lo.push(null); hi.push(null); }
    }
    var ys = s.values.concat(lo, hi).filter(function (v) { return v != null; });
    if (!ys.length) { host.innerHTML = '<div class="empty-chart">No values</div>'; return; }
    var f = frame(host, { times: times, ys: ys, height: o.height });
    var band = "", seg = [], line = "", marks = "", points = [];
    for (var j = 0; j < n; j++) {
      if (lo[j] != null) seg.push(j);
      if ((lo[j] == null || j === n - 1) && seg.length) {
        if (seg.length > 1) {
          band += '<path class="band" d="M' + seg.map(function (k) { return f.x(times[k]) + "," + f.y(hi[k]); }).join("L") +
            "L" + seg.slice().reverse().map(function (k) { return f.x(times[k]) + "," + f.y(lo[k]); }).join("L") + 'Z"/>';
        }
        seg = [];
      }
    }
    var pen = false, d = "";
    for (var q = 0; q < n; q++) {
      var v = s.values[q];
      if (v == null) { pen = false; points.push({ x: f.x(times[q]) }); continue; }
      var px = f.x(times[q]), py = f.y(v);
      d += (pen ? "L" : "M") + px + "," + py;
      pen = true;
      points.push({ x: px, y: py });
      var flag = s.flags ? s.flags[q] : 0;
      if (flag & 1) {
        marks += '<g><title>' + esc("Anomaly on " + D.dates[s.dates[q]] + ": " + num(v) +
          (lo[q] != null ? ", expected " + num(lo[q]) + " – " + num(hi[q]) : "")) + "</title>" +
          '<circle class="anomaly-ring" cx="' + px + '" cy="' + py + '" r="8"/>' +
          '<circle class="anomaly" cx="' + px + '" cy="' + py + '" r="4.5" aria-label="' +
          esc("Anomaly on " + D.dates[s.dates[q]] + ": " + num(v)) + '"/></g>';
      } else if (flag & 2) {
        marks += '<circle class="err" cx="' + px + '" cy="' + py + '" r="4.5"><title>Error on ' + esc(D.dates[s.dates[q]]) + "</title></circle>";
      } else if (n <= 45) {
        marks += '<circle class="pt" cx="' + px + '" cy="' + py + '" r="3"/>';
      }
    }
    line = '<path class="line" d="' + d + '"/>';
    var anomalies = (s.flags || []).filter(function (x) { return x & 1; }).length;
    var label = (o.label || "Series") + ", " + n + " points" + (anomalies ? ", " + anomalies + " anomalous" : "");
    host.innerHTML = '<svg viewBox="0 0 ' + f.W + " " + f.H + '" height="' + f.H + '" role="img" tabindex="0" aria-label="' + esc(label) + '">' +
      f.axes + band + line + '<line class="cross" y1="' + f.M.t + '" y2="' + (f.H - f.M.b) + '" hidden/>' + marks + "</svg>";
    attachHover(host, f, points, function (i) {
      var rows = '<div class="row"><span>Value</span><span>' + esc(num(s.values[i])) + (o.unit ? " " + esc(o.unit) : "") + "</span></div>";
      if (lo[i] != null) rows += '<div class="row"><span>Expected</span><span>' + esc(num(lo[i])) + " – " + esc(num(hi[i])) + "</span></div>";
      var fl = s.flags ? s.flags[i] : 0;
      if (fl & 1) rows += '<div class="row"><span>Status</span><span style="color:var(--amber)">Anomaly</span></div>';
      else if (fl & 4 && s.means) rows += '<div class="row"><span>Status</span><span>Training</span></div>';
      return "<b>" + esc(D.dates[s.dates[i]]) + "</b>" + rows;
    });
  }

  function barChart(host, rows, o) {
    // rows: [[dateIndex, value]]; bars only on days that have data.
    if (!rows.length) { host.innerHTML = '<div class="empty-chart">No data</div>'; return; }
    var times = rows.map(function (r) { return time(D.dates[r[0]]); });
    var ys = rows.map(function (r) { return r[1]; });
    var spanDays = Math.max(1, Math.round((Math.max.apply(null, times) - Math.min.apply(null, times)) / 864e5) + 1);
    var f = frame(host, { times: times, ys: ys.concat([0, 1]), zero: true, height: o.height || 150, padTime: 432e5 });
    var plot = f.W - f.M.l - f.M.r;
    var bw = Math.max(2, Math.min(18, (plot / spanDays) * 0.66));
    var bars = "", points = [];
    rows.forEach(function (r, i) {
      var x = f.x(times[i]), top = f.y(r[1]), base = f.y(f.y0);
      points.push({ x: x, y: top });
      if (r[1] <= 0) return;
      var rad = Math.min(3, bw / 2, base - top);
      bars += '<path class="bar ' + (o.cls || "") + '" d="M' + (x - bw / 2) + "," + base + "V" + (top + rad) +
        "Q" + (x - bw / 2) + "," + top + " " + (x - bw / 2 + rad) + "," + top + "H" + (x + bw / 2 - rad) +
        "Q" + (x + bw / 2) + "," + top + " " + (x + bw / 2) + "," + (top + rad) + "V" + base + 'Z"/>';
    });
    host.innerHTML = '<svg viewBox="0 0 ' + f.W + " " + f.H + '" height="' + f.H + '" role="img" tabindex="0" aria-label="' +
      esc(o.label + ": " + rows.length + " days, latest " + num(ys[ys.length - 1])) + '">' + f.axes +
      '<line class="cross" y1="' + f.M.t + '" y2="' + (f.H - f.M.b) + '" hidden/>' + bars + "</svg>";
    attachHover(host, f, points, function (i) {
      return "<b>" + esc(D.dates[rows[i][0]]) + '</b><div class="row"><span>' + esc(o.unit) + "</span><span>" +
        esc(num(rows[i][1])) + "</span></div>";
    });
  }

  function donut(segments, total, unit) {
    var r = 46, c = 2 * Math.PI * r, sum = 0, out = "";
    segments.forEach(function (s) { sum += s.v; });
    var gap = segments.filter(function (s) { return s.v > 0; }).length > 1 ? 2 : 0, offset = 0;
    segments.forEach(function (s) {
      if (!s.v) return;
      var len = Math.max(0, (s.v / sum) * c - gap);
      if (len < 1.5) len = 1.5;
      out += '<circle cx="60" cy="60" r="' + r + '" fill="none" stroke="' + s.color + '" stroke-width="12" stroke-dasharray="' +
        len + " " + (c - len) + '" stroke-dashoffset="' + -offset + '" transform="rotate(-90 60 60)"/>';
      offset += (s.v / sum) * c;
    });
    if (!sum) out = '<circle cx="60" cy="60" r="' + r + '" fill="none" stroke="var(--raised)" stroke-width="12"/>';
    return '<svg class="donut" viewBox="0 0 120 120" role="img" aria-label="' + esc(num(total) + " " + unit) + '">' + out +
      '<text class="total" x="60" y="62" text-anchor="middle">' + esc(total < 1e5 ? num(total) : compact(total)) + "</text>" +
      '<text class="unit" x="60" y="79" text-anchor="middle">' + esc(unit) + "</text></svg>";
  }

  var HEALTH_WORD = ["pass", "non-blocking fail", "blocking fail", "error", "skipped"];
  function strip(dates, codes, big, limit) {
    var start = limit ? Math.max(0, codes.length - limit) : 0, out = "";
    for (var i = start; i < codes.length; i++) {
      out += '<i class="s' + codes[i] + '" title="' + esc(D.dates[dates[i]] + ": " + HEALTH_WORD[codes[i]]) + '"></i>';
    }
    return '<span class="strip' + (big ? " big" : "") + '" role="img" aria-label="' +
      esc((codes.length - start) + " intervals, latest " + (codes.length ? HEALTH_WORD[codes[codes.length - 1]] : "none")) + '">' + out + "</span>";
  }
  var HEALTH_KEY = '<div class="key">' + [["var(--green-mark)", "Pass", ".55"], ["var(--amber-mark)", "Non-blocking fail"],
    ["var(--red-mark)", "Blocking fail or error"], ["var(--grey)", "Skipped", ".5"]].map(function (k) {
    return '<span><i style="background:' + k[0] + (k[2] ? ";opacity:" + k[2] : "") + '"></i>' + k[1] + "</span>";
  }).join("") + "</div>";

  function chartCard(title, code, draw, tableRows, tableHead) {
    var card = h("section", { class: "panel chart-card" });
    card.innerHTML = '<div class="panel-head"><h3>' + esc(title) + "</h3>" + (code ? "<code>" + esc(code) + "</code>" : "") +
      '<div class="end"><button type="button" class="btn" aria-pressed="false" title="Show as table">' + ICON.table +
      '<span class="label">Table</span></button></div></div><div class="panel-body"><div class="chart"></div></div>';
    var body = card.querySelector(".panel-body"), host = card.querySelector(".chart"), btn = card.querySelector("button");
    var drawn = false;
    btn.addEventListener("click", function () {
      var on = btn.getAttribute("aria-pressed") !== "true";
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      if (on) {
        body.innerHTML = '<div class="chart-scroll"><table class="chart-table"><thead><tr>' +
          tableHead.map(function (c) { return "<th>" + esc(c) + "</th>"; }).join("") + "</tr></thead><tbody>" +
          tableRows().map(function (r) { return "<tr>" + r.map(function (c) { return "<td>" + c + "</td>"; }).join("") + "</tr>"; }).join("") +
          "</tbody></table></div>";
      } else {
        body.innerHTML = "";
        host = h("div", { class: "chart" });
        body.appendChild(host);
        draw(host);
      }
    });
    card.draw = function () { if (!drawn) { drawn = true; draw(host); } };
    return card;
  }

  function seriesCard(s) {
    return chartCard(metricLabel(s.metric, s.column), s.metric, function (host) {
      lineChart(host, s, { label: metricLabel(s.metric, s.column) });
    }, function () {
      return s.values.map(function (v, i) {
        var mean = s.means[i], sd = s.sds[i];
        var exp = mean != null && sd != null ? esc(num(mean - s.k * sd)) + " – " + esc(num(mean + s.k * sd)) : "-";
        return [esc(D.dates[s.dates[i]]), esc(num(v)), exp, s.flags[i] & 1 ? '<span class="pill warn">anomaly</span>' : ""];
      }).reverse();
    }, ["Interval", "Value", "Expected", ""]);
  }

  // ---------------------------------------------------------------- windowed list

  function List(host, o) {
    // o: {cols:[{label, w, num, sort}], rows, cells(row)->[html], expand(row)->Node, href(row), group(row)->label,
    //     sort:{i, dir}, empty, min, onCount, key(row)}
    var cols = o.cols.map(function (c) { return c.w || "minmax(0,1fr)"; }).join(" ");
    host.innerHTML = '<div class="list"><div class="list-inner" style="--cols:' + cols + ";--min:" + (o.min || 720) + 'px">' +
      '<div class="list-head" role="row"></div><div class="list-body" role="rowgroup"></div></div></div>';
    var scroller = host.querySelector(".list"), head = host.querySelector(".list-head"), body = host.querySelector(".list-body");
    var items = [], sort = o.sort || null, expanded = -1, expNode = null, expH = 0, frameReq = 0;
    scroller.setAttribute("role", "table");

    function drawHead() {
      head.innerHTML = o.cols.map(function (c, i) {
        var s = sort && sort.i === i ? (sort.dir > 0 ? "ascending" : "descending") : null;
        var arrow = s ? (s === "ascending" ? " ↑" : " ↓") : "";
        var inner = c.sort && !o.group ? '<button type="button"' + (s ? ' aria-sort="' + s + '"' : "") + ' data-i="' + i + '">' +
          esc(c.label) + arrow + "</button>" : esc(c.label);
        return '<div role="columnheader" class="' + (c.num ? "num" : "") + '">' + inner + "</div>";
      }).join("");
    }
    head.addEventListener("click", function (e) {
      var b = e.target.closest("button");
      if (!b) return;
      var i = +b.dataset.i;
      sort = sort && sort.i === i ? { i: i, dir: -sort.dir } : { i: i, dir: o.cols[i].num ? -1 : 1 };
      if (o.onSort) o.onSort(sort);
      set(o.rows);
    });

    function sorted(rows) {
      if (!sort || o.group) return rows.slice();
      var f = o.cols[sort.i].sort;
      return rows.slice().sort(function (a, b) {
        var x = f(a), y = f(b);
        return (x < y ? -1 : x > y ? 1 : 0) * sort.dir;
      });
    }
    function set(rows) {
      o.rows = rows;
      var list = sorted(rows);
      items = [];
      if (o.group) {
        var last = null;
        list.forEach(function (r) {
          var g = o.group(r);
          if (g !== last) { items.push({ g: g, n: list.filter(function (x) { return o.group(x) === g; }).length }); last = g; }
          items.push(r);
        });
      } else items = list;
      collapse();
      drawHead();
      layout();
      if (o.onCount) o.onCount(rows.length);
    }
    function collapse() { expanded = -1; expNode = null; expH = 0; }
    function topOf(i) { return i * RH + (expanded >= 0 && i > expanded ? expH : 0); }
    function layout() {
      body.style.height = (items.length * RH + (expanded >= 0 ? expH : 0)) + "px";
      draw();
    }
    function draw() {
      frameReq = 0;
      if (!items.length) {
        body.style.height = "auto";
        body.innerHTML = '<div class="list-empty">' + esc(o.empty || "Nothing to show") + "</div>";
        return;
      }
      var view = scroller.clientHeight || 600;
      var top = Math.max(0, scroller.scrollTop - 34);
      var first = Math.max(0, Math.floor(top / RH) - 20);
      if (expanded >= 0 && top > topOf(expanded) + RH) first = Math.max(0, Math.floor((top - expH) / RH) - 20);
      var last = Math.min(items.length, first + Math.ceil(view / RH) + 40);
      var html = "";
      for (var i = first; i < last; i++) {
        var it = items[i], y = topOf(i);
        if (it && it.g !== undefined) {
          html += '<div class="list-group" style="top:' + y + 'px">' + esc(it.g) + ' <span class="count">' + num(it.n) + "</span></div>";
          continue;
        }
        var clickable = o.expand || o.href;
        html += '<div class="list-row' + (clickable ? " clickable" : "") + '" role="row" data-i="' + i + '" style="top:' + y + 'px"' +
          (clickable ? ' tabindex="0"' : "") + (o.expand ? ' aria-expanded="' + (i === expanded) + '"' : "") + ">" +
          o.cells(it).map(function (c, k) {
            return '<div role="cell" class="' + (o.cols[k].num ? "num" : "") + '">' + c + "</div>";
          }).join("") + "</div>";
      }
      body.innerHTML = html;
      if (expanded >= 0 && expNode) {
        expNode.style.top = topOf(expanded) + RH + "px";
        body.appendChild(expNode);
      }
    }
    scroller.addEventListener("scroll", function () { if (!frameReq) frameReq = requestAnimationFrame(draw); });
    function toggle(i) {
      var it = items[i];
      if (o.href) { location.hash = o.href(it); return; }
      if (!o.expand) return;
      if (expanded === i) { collapse(); layout(); return; }
      collapse();
      expanded = i;
      expNode = h("div", { class: "list-detail" });
      var content = o.expand(it);
      if (typeof content === "string") expNode.innerHTML = content; else expNode.appendChild(content);
      body.appendChild(expNode);
      expNode.style.top = topOf(i) + RH + "px";
      drawCharts(expNode);
      expH = expNode.offsetHeight;
      expNode.addEventListener("toggle", function () { expH = expNode.offsetHeight; layout(); }, true);
      layout();
    }
    body.addEventListener("click", function (e) {
      if (e.target.closest("a")) return;
      var row = e.target.closest(".list-row");
      if (row) toggle(+row.dataset.i);
    });
    body.addEventListener("keydown", function (e) {
      var row = e.target.closest(".list-row");
      if (row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggle(+row.dataset.i); }
    });
    this.set = set;
    this.open = function (pred) {
      var i = items.findIndex(function (it) { return it && it.g === undefined && pred(it); });
      if (i < 0) return;
      toggle(i);
      scroller.scrollTop = Math.max(0, topOf(i) - 40);
      draw();
      var r = host.getBoundingClientRect();
      if (r.top < 0 || r.top > innerHeight) host.scrollIntoView({ block: "start" });
    };
    this.rendered = function () { return body.querySelectorAll(".list-row").length; };
    set(o.rows);
  }

  function drawCharts(root) {
    root.querySelectorAll(".chart-card").forEach(function (c) { if (c.draw) c.draw(); });
  }

  // ---------------------------------------------------------------- shared row pieces

  function modelLink(m) {
    return '<a href="#model?name=' + encodeURIComponent(m.name) + '"><code>' + esc(m.display) + "</code></a>";
  }
  function incidentDetail(inc, opts) {
    var m = models[inc.model], wrap = h("div");
    var kind = inc.status === "error" ? "error" : inc.blocking ? "fail" : "warn";
    var facts = [
      ["Model", m ? modelLink(m) : "-"], ["Audit", useLabel(inc.use)], ["Status", kindPill(kind)],
      ["Interval", esc(inc.interval || "-")], ["Violations", esc(inc.violations == null ? "-" : num(inc.violations))],
      ["Environment", esc(inc.environment || "-")], ["Snapshot", "<code>" + esc(inc.snapshot || "-") + "</code>"],
      ["Version", "<code>" + esc(inc.version || "-") + "</code>"],
      ["Invocation", "<code>" + esc((inc.invocation || "-").slice(0, 12)) + "</code>"],
      ["Started", esc(inc.started_at || "-")]
    ];
    var html = '<dl class="detail-grid">' + facts.map(function (f) { return "<div><dt>" + f[0] + "</dt><dd>" + f[1] + "</dd></div>"; }).join("") + "</dl>";
    if (inc.error) html += '<p class="detail-error">' + esc(inc.error) + "</p>";
    html += '<div class="detail-chart"></div>';
    if (inc.samples && inc.samples.length) {
      var keys = [];
      inc.samples.forEach(function (s) { if (s && typeof s === "object") Object.keys(s).forEach(function (k) { if (keys.indexOf(k) < 0) keys.push(k); }); });
      html += '<div class="samples"><h4>Sample rows</h4><table><thead><tr>' + keys.map(function (k) { return "<th>" + esc(k) + "</th>"; }).join("") +
        "</tr></thead><tbody>" + inc.samples.map(function (s) {
          return "<tr>" + keys.map(function (k) {
            var v = s && typeof s === "object" ? s[k] : s;
            return "<td>" + (v === null ? '<span class="null">null</span>' : esc(typeof v === "object" ? JSON.stringify(v) : v)) + "</td>";
          }).join("") + "</tr>";
        }).join("") + "</tbody></table></div>";
    }
    if (inc.sql) html += '<details class="sql-wrap"><summary>Audit query</summary><pre class="sql">' + esc(inc.sql) + "</pre></details>";
    wrap.innerHTML = html;
    if (opts && opts.series) wrap.querySelector(".detail-chart").appendChild(seriesCard(opts.series));
    return wrap;
  }
  function seriesDetail(m, s) {
    var wrap = h("div");
    wrap.innerHTML = '<p class="muted" style="margin:0 0 10px">' + modelLink(m) + " · " + esc(metricLabel(s.metric, s.column)) + "</p>";
    wrap.appendChild(seriesCard(s));
    return wrap;
  }

  // ---------------------------------------------------------------- shell

  var route = { view: "overview", params: {} };
  function parse() {
    var raw = location.hash.slice(1) || document.body.getAttribute("data-start") || "overview";
    var q = raw.indexOf("?"), params = {};
    if (q >= 0) raw.slice(q + 1).split("&").forEach(function (kv) {
      if (!kv) return;
      var p = kv.split("=");
      params[decodeURIComponent(p[0])] = decodeURIComponent((p[1] || "").replace(/\+/g, " "));
    });
    return { view: q >= 0 ? raw.slice(0, q) : raw, params: params };
  }
  function hashFor(view, params) {
    var q = Object.keys(params || {}).filter(function (k) { return params[k] != null && params[k] !== ""; })
      .map(function (k) { return encodeURIComponent(k) + "=" + encodeURIComponent(params[k]); }).join("&");
    return "#" + view + (q ? "?" + q : "");
  }
  function keep(params) {
    // Filter changes update the URL without re-rendering the view.
    route.params = params;
    history.replaceState(null, "", hashFor(route.view, params));
  }

  var counts = {
    models: models.length, tests: allTests().length,
    incidents: D.incidents.length, runs: D.runs.length,
    schema: models.reduce(function (n, m) { return n + m.raw.x.length; }, 0)
  };
  var failing = D.summary.models.fail, warning = D.summary.models.warn;
  var TABS = [
    ["overview", "Dashboard", null],
    ["models", "Models", '<span class="count' + (failing ? " fail" : warning ? " warn" : "") + '">' + num(counts.models) + "</span>"],
    ["tests", "Test results", '<span class="count">' + num(D.summary.audits.pass + D.summary.audits.warn + D.summary.audits.fail + D.summary.audits.error) + "</span>"],
    ["incidents", "Incidents", counts.incidents ? '<span class="count fail">' + num(counts.incidents) + "</span>" : null],
    ["runs", "Runs", null],
    ["schema", "Schema changes", counts.schema ? '<span class="count">' + num(counts.schema) + "</span>" : null]
  ];

  function lastRunChip() {
    var r = D.runs[0];
    if (!r) return "";
    var st = r.blocking_failures || r.status === "failure" || r.failed_runs ? "fail" : r.failing_audits || r.status === "unfinished" ? "warn" : "pass";
    return '<a class="chip" href="#runs" title="Last run ' + esc(r.started_at || "") + ' UTC"><span class="dot ' + st + '"></span>' +
      '<span class="label">Last ' + esc(r.command || "run") + " · </span>" + esc(clock(r.started_at)) + "</a>";
  }

  document.title = "sqlmesh-observe · " + D.environment;
  var app = document.getElementById("app");
  app.innerHTML =
    '<header class="top"><button type="button" class="btn tree-toggle" aria-label="Models" aria-expanded="false">' + ICON.tree + "</button>" +
    '<a class="brand" href="#overview">' + MARK + "<span>sqlmesh-observe</span></a>" +
    '<nav class="tabs" aria-label="Views">' + TABS.map(function (t) {
      return '<a href="#' + t[0] + '" data-view="' + t[0] + '">' + t[1] + (t[2] || "") + "</a>";
    }).join("") + "</nav>" +
    '<div class="top-end"><div class="jump"><input class="field" type="search" placeholder="Jump to model" aria-label="Jump to model" autocomplete="off">' +
    '<div class="jump-list" hidden role="listbox"></div></div>' + lastRunChip() +
    '<button type="button" class="btn theme" aria-label="Theme">' + ICON.theme + '<span class="label">Theme</span></button></div></header>' +
    '<div class="layout"><aside class="tree" aria-label="Models"></aside><main><div class="page"></div></main></div>';
  var page = app.querySelector(".page"), layoutEl = app.querySelector(".layout"), treeEl = app.querySelector(".tree");

  // Theme: system until the reader picks; the pick is remembered.
  var saved = storeGet(THEME_KEY);
  if (saved === "light" || saved === "dark") document.documentElement.setAttribute("data-theme", saved);
  app.querySelector(".theme").addEventListener("click", function () {
    var cur = document.documentElement.getAttribute("data-theme") ||
      (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
    var next = cur === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", next);
    storeSet(THEME_KEY, next);
  });

  // Jump to model
  (function () {
    var input = app.querySelector(".jump input"), list = app.querySelector(".jump-list"), sel = 0, hits = [];
    function paint() {
      list.innerHTML = hits.length ? hits.map(function (m, i) {
        return '<a role="option" href="#model?name=' + encodeURIComponent(m.name) + '" aria-selected="' + (i === sel) + '"><span class="dot ' + m.st + '"></span>' + esc(m.display) + "</a>";
      }).join("") : '<div class="none">No matching models</div>';
    }
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      list.hidden = !q;
      if (!q) return;
      hits = models.filter(function (m) { return m.short.toLowerCase().indexOf(q) >= 0; }).slice(0, 20);
      sel = 0;
      paint();
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { sel = Math.min(hits.length - 1, sel + 1); paint(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { sel = Math.max(0, sel - 1); paint(); e.preventDefault(); }
      else if (e.key === "Enter" && hits[sel]) { location.hash = "model?name=" + encodeURIComponent(hits[sel].name); input.value = ""; list.hidden = true; input.blur(); }
      else if (e.key === "Escape") { input.value = ""; list.hidden = true; input.blur(); }
    });
    input.addEventListener("blur", function () { setTimeout(function () { list.hidden = true; }, 150); });
    list.addEventListener("click", function () { input.value = ""; list.hidden = true; });
    document.addEventListener("keydown", function (e) {
      if (e.key === "/" && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) { e.preventDefault(); input.focus(); }
    });
  })();

  // ---------------------------------------------------------------- tree

  var treeMode = storeGet(TREE_KEY) === "owner" ? "owner" : "schema";
  var treeOpen = {}, treeQuery = "";
  treeEl.innerHTML = '<div class="tree-head"><div class="seg" role="group" aria-label="Group models by">' +
    '<button type="button" data-mode="schema">Schemas</button><button type="button" data-mode="owner">Owners</button></div>' +
    '<input class="field" type="search" placeholder="Filter models" aria-label="Filter models"></div><div class="tree-body"></div>';
  var treeBody = treeEl.querySelector(".tree-body");
  function treeGroups() {
    var groups = {};
    models.forEach(function (m) {
      var g = treeMode === "owner" ? (m.owner || "No owner") : m.group;
      (groups[g] = groups[g] || []).push(m);
    });
    return groups;
  }
  function badges(list) {
    var c = { pass: 0, warn: 0, fail: 0 };
    list.forEach(function (m) { c[m.st]++; });
    return '<span class="badges">' + ["pass", "warn", "fail"].map(function (k) {
      return c[k] ? '<span class="badge ' + k + '" title="' + c[k] + " " + STATUS_WORD[k] + '">' + num(c[k]) + "</span>" : "";
    }).join("") + "</span>";
  }
  function drawTree() {
    treeEl.querySelectorAll(".seg button").forEach(function (b) { b.setAttribute("aria-pressed", b.dataset.mode === treeMode); });
    var groups = treeGroups(), q = treeQuery.toLowerCase(), html = "", current = route.view === "model" ? route.params.name : null;
    Object.keys(groups).sort().forEach(function (g) {
      var all = groups[g];
      var shown = q ? all.filter(function (m) { return m.short.toLowerCase().indexOf(q) >= 0; }) : all;
      if (!shown.length) return;
      var key = treeMode + ":" + g;
      var open = q ? true : treeOpen[key] != null ? treeOpen[key] :
        all.some(function (m) { return m.st === "fail" || m.name === current; });
      shown = shown.slice().sort(function (a, b) { return RANK[a.st] - RANK[b.st] || (a.leaf < b.leaf ? -1 : 1); });
      var limit = 200, leaves = "";
      if (open) {
        leaves = shown.slice(0, limit).map(function (m) {
          return '<a class="tree-leaf" href="#model?name=' + encodeURIComponent(m.name) + '"' + (m.name === current ? ' aria-current="page"' : "") +
            '><span class="dot ' + m.st + '"></span><span class="label">' + esc(treeMode === "owner" ? m.display : m.leaf) + "</span></a>";
        }).join("");
        if (shown.length > limit) leaves += '<div class="tree-more">' + num(shown.length - limit) + " more. Filter to narrow.</div>";
      }
      html += '<div class="tree-group"' + (open ? " data-open" : "") + ' data-key="' + esc(key) + '"><button type="button" aria-expanded="' + open + '">' +
        ICON.chev + '<span class="label">' + esc(g) + "</span>" + badges(all) + '</button><div class="leaves">' + leaves + "</div></div>";
    });
    treeBody.innerHTML = html || '<div class="tree-empty">No matching models</div>';
  }
  treeEl.querySelector(".seg").addEventListener("click", function (e) {
    var b = e.target.closest("button");
    if (!b) return;
    treeMode = b.dataset.mode;
    storeSet(TREE_KEY, treeMode);
    drawTree();
  });
  treeEl.querySelector("input").addEventListener("input", function (e) { treeQuery = e.target.value.trim(); drawTree(); });
  treeBody.addEventListener("click", function (e) {
    var b = e.target.closest(".tree-group > button");
    if (!b) return;
    var g = b.parentNode, key = g.dataset.key;
    treeOpen[key] = !g.hasAttribute("data-open");
    drawTree();
  });
  var treeToggle = app.querySelector(".tree-toggle");
  treeToggle.addEventListener("click", function () {
    var open = !treeEl.hasAttribute("data-open");
    if (open) treeEl.setAttribute("data-open", ""); else treeEl.removeAttribute("data-open");
    treeToggle.setAttribute("aria-expanded", open);
  });

  // ---------------------------------------------------------------- views

  function select(name, label, options, value) {
    return '<select class="field" name="' + name + '" aria-label="' + esc(label) + '">' + options.map(function (o) {
      return '<option value="' + esc(o[0]) + '"' + (String(o[0]) === String(value || "") ? " selected" : "") + ">" + esc(o[1]) + "</option>";
    }).join("") + "</select>";
  }
  function seg(name, options, value) {
    return '<div class="seg" role="group" data-name="' + name + '">' + options.map(function (o) {
      return '<button type="button" data-v="' + esc(o[0]) + '" aria-pressed="' + (String(o[0]) === String(value || "")) + '">' + esc(o[1]) + "</button>";
    }).join("") + "</div>";
  }
  function bindFilters(root, apply) {
    root.querySelectorAll("input.field, select.field").forEach(function (el) {
      el.addEventListener(el.tagName === "SELECT" ? "change" : "input", apply);
    });
    root.querySelectorAll(".filters .seg").forEach(function (g) {
      g.addEventListener("click", function (e) {
        var b = e.target.closest("button");
        if (!b) return;
        g.querySelectorAll("button").forEach(function (x) { x.setAttribute("aria-pressed", x === b); });
        apply();
      });
    });
  }
  function filterValues(root) {
    var v = {};
    root.querySelectorAll("input.field, select.field").forEach(function (el) { v[el.name] = el.value.trim(); });
    root.querySelectorAll(".filters .seg").forEach(function (g) {
      var on = g.querySelector('[aria-pressed="true"]');
      v[g.dataset.name] = on ? on.dataset.v : "";
    });
    return v;
  }
  function panel(title, extra) {
    var p = h("section", { class: "panel" });
    p.innerHTML = '<div class="panel-head"><h2>' + title + "</h2>" + (extra || "") + "</div>";
    return p;
  }

  // Dashboard ----------------------------------------------------------------

  function viewOverview() {
    layoutEl.classList.add("wide");
    var s = D.summary, st = failing ? "fail" : warning ? "warn" : "pass";
    var word = failing ? plural(failing, "failing model") : warning ? plural(warning, "model") + " with warnings" : "Healthy";
    page.innerHTML =
      '<div class="headline"><h1>' + esc(D.environment) + '</h1><span class="pill ' + st + '">' + esc(word) + "</span>" +
      '<span class="meta">Generated ' + esc(D.generated_at) + "</span>" +
      "</div>" +
      (D.state_available ? "" : '<div class="state-warning">SQLMesh state for ' + esc(D.environment) +
        " could not be read. Showing only the latest invocation that targeted it.</div>");

    var a = s.audits, auditTotal = a.pass + a.warn + a.fail + a.error;
    function legend(rows) {
      return '<ul class="legend">' + rows.map(function (r) {
        var v = r[2] ? '<a href="' + r[3] + '">' + num(r[2]) + "</a>" : '<span class="v">0</span>';
        return '<li><span class="dot" style="background:' + r[0] + '"></span><span>' + r[1] + "</span>" + v + "</li>";
      }).join("") + "</ul>";
    }
    var summary = h("section", { class: "panel" });
    summary.innerHTML = '<div class="summary">' +
      "<div><h2>Audit results</h2>" + donut([
        { v: a.pass, color: "var(--green-mark)" }, { v: a.warn, color: "var(--amber-mark)" },
        { v: a.fail, color: "var(--red-mark)" }, { v: a.error, color: "var(--grey)" }], auditTotal, "audits") +
      legend([["var(--green-mark)", "Passed", a.pass, "#tests?status=pass"], ["var(--amber-mark)", "Non-blocking fail", a.warn, "#tests?status=warn"],
        ["var(--red-mark)", "Blocking fail", a.fail, "#tests?status=fail"], ["var(--grey)", "Error", a.error, "#tests?status=error"]]) + "</div>" +
      "<div><h2>Model health</h2>" + donut([
        { v: s.models.pass, color: "var(--green-mark)" }, { v: s.models.warn, color: "var(--amber-mark)" }, { v: s.models.fail, color: "var(--red-mark)" }],
        models.length, "models") +
      legend([["var(--green-mark)", "Healthy", s.models.pass, "#models?status=pass"], ["var(--amber-mark)", "Warning", s.models.warn, "#models?status=warn"],
        ["var(--red-mark)", "Failing", s.models.fail, "#models?status=fail"]]) + "</div>" +
      "<div><h2>Monitored models</h2>" + donut([
        { v: s.monitored.monitored, color: "var(--series)" }, { v: s.monitored.unmonitored, color: "var(--grey)" }], models.length, "models") +
      legend([["var(--series)", "With monitors", s.monitored.monitored, "#models?monitored=1"],
        ["var(--grey)", "Without", s.monitored.unmonitored, "#models?monitored=0"]]) + "</div></div>" +
      '<div class="signals">' + [
        ["audits", "Audits", "#tests?status=", "audits"],
        ["volume", "Volume", "#models?sig=volume&sst=", "models"],
        ["freshness", "Freshness", "#models?sig=freshness&sst=", "models"],
        ["schema", "Schema changes", "#models?sig=schema&sst=", "models"],
        ["columns", "Column metrics", "#models?sig=columns&sst=", "metrics"]
      ].map(function (sg) {
        var c = D.signals[sg[0]];
        if (!c) return '<div class="signal"><h3>' + sg[1] + '</h3><div class="off">Not configured</div></div>';
        var total = c.pass + c.warn + c.fail;
        var split = ["pass", "warn", "fail"].map(function (k) {
          return c[k] ? '<a class="' + k + '" href="' + sg[2] + k + (sg[0] === "audits" ? "" : "") + '" title="' + esc(num(c[k]) + " " + k) + '">' + num(c[k]) + "</a>" :
            '<span class="zero">0</span>';
        }).join("");
        return '<div class="signal"><h3>' + sg[1] + '</h3><div class="total">' + num(total) + "<small>" + sg[3] + '</small></div><div class="split">' + split + "</div></div>";
      }).join("") + "</div>";
    page.appendChild(summary);

    var trends = h("div", { class: "grid three" });
    var series = [
      ["Audit results per day", 1, "", "Audits"],
      ["Failures per day", 2, "fail", "Blocking failures"],
      ["Warnings and anomalies per day", 3, "warn", "Warnings and anomalies"]
    ];
    page.appendChild(trends);
    series.forEach(function (sr) {
      var p = h("section", { class: "panel chart-card" });
      p.innerHTML = '<div class="panel-head"><h3>' + sr[0] + '</h3></div><div class="panel-body"><div class="chart"></div></div>';
      trends.appendChild(p);
      var rows = D.trend.map(function (r) { return [r[0], r[sr[1]]]; });
      barChart(p.querySelector(".chart"), rows, { cls: sr[2], label: sr[0], unit: sr[3] });
    });

    var issues = D.issues;
    var n = { all: issues.length, blocking: 0, anomaly: 0, warning: 0 };
    issues.forEach(function (i) { n[i.kind === "error" ? "blocking" : i.kind]++; });
    var filter = route.params.issues || "all";
    var p = panel("Open issues", '<span class="pill ' + (n.blocking ? "fail" : issues.length ? "warn" : "pass") + '">' + num(issues.length) + " open</span>" +
      '<div class="end filters" style="border:0;padding:0">' + seg("issues", [["all", "All " + n.all], ["blocking", "Blocking " + n.blocking],
        ["anomaly", "Anomalies " + n.anomaly], ["warning", "Warnings " + n.warning]], filter) + "</div>");
    var host = h("div");
    p.appendChild(host);
    page.appendChild(p);
    var GROUP = { blocking: "Blocking failures and errors", error: "Blocking failures and errors", anomaly: "Anomalies at the latest interval", warning: "Non-blocking failures" };
    function rowsFor(f) { return issues.filter(function (i) { return f === "all" || i.kind === f || (f === "blocking" && i.kind === "error"); }); }
    var list = new List(host, {
      cols: [{ label: "Model", w: "minmax(200px,1.3fr)" }, { label: "Signal", w: "minmax(180px,1.4fr)" }, { label: "Interval", w: "110px" },
        { label: "Value", w: "minmax(120px,0.8fr)", num: true }, { label: "Severity", w: "110px" }],
      rows: rowsFor(filter), group: function (i) { return GROUP[i.kind]; }, min: 760, empty: "No open issues",
      cells: function (i) {
        var m = models[i.model];
        var sig = i.kind === "anomaly" ? esc(metricLabel(i.signal, i.column)) + ' <span class="args"><code>' + esc(i.signal) + "</code></span>" : useLabel(i.use);
        var val = i.kind === "anomaly" ? '<span class="strong">' + esc(num(i.value)) + '</span><span class="exp">expected ' + esc(num(i.lo)) + " – " + esc(num(i.hi)) + "</span>" :
          '<span class="strong">' + esc(i.value == null ? "-" : plural(i.value, "row")) + '</span><span class="exp">expected 0</span>';
        var sev = i.kind === "anomaly" ? '<span class="pill warn">anomaly</span>' : kindPill(i.kind === "warning" ? "warn" : i.kind === "error" ? "error" : "fail");
        return [m ? modelLink(m) + (m.owner ? ' <span class="sub">' + esc(m.owner) + "</span>" : "") : "-", sig, '<span class="mono">' + esc(i.interval || "-") + "</span>", val, sev];
      },
      expand: function (i) {
        var m = models[i.model];
        if (i.kind === "anomaly") {
          var s = m && m.series.find(function (x) { return x.metric === i.signal && x.column === (i.column || ""); });
          return s ? seriesDetail(m, s) : "No chart for this series";
        }
        var inc = D.incidents[i.incident];
        return inc ? incidentDetail(inc, { series: m && seriesFor(m, inc.use) }) : "No stored detail";
      }
    });
    p.querySelector(".seg").addEventListener("click", function (e) {
      var b = e.target.closest("button");
      if (!b) return;
      p.querySelectorAll(".seg button").forEach(function (x) { x.setAttribute("aria-pressed", x === b); });
      keep({ issues: b.dataset.v === "all" ? "" : b.dataset.v });
      list.set(rowsFor(b.dataset.v));
    });
  }

  // Models -------------------------------------------------------------------

  function viewModels() {
    var P = route.params;
    var p = panel("Models", '<div class="end"><span class="shown"></span></div>');
    var filters = h("div", { class: "filters" });
    filters.innerHTML = '<input class="field" type="search" name="q" placeholder="Filter by name" aria-label="Filter by name" value="' + esc(P.q || "") + '">' +
      select("status", "Status", [["", "Any status"], ["fail", "Failing"], ["warn", "Warning"], ["pass", "Healthy"]], P.status) +
      select("owner", "Owner", [["", "Any owner"]].concat(owners.map(function (o) { return [o, o]; })), P.owner) +
      select("schema", "Schema", [["", "Any schema"]].concat(schemas.map(function (s) { return [s, s]; })), P.schema) +
      (P.sig ? '<span class="chip">' + esc({ volume: "Volume", freshness: "Freshness", schema: "Schema", columns: "Column metrics" }[P.sig] || P.sig) +
        ": " + esc(STATUS_WORD[P.sst] || "any") + ' <a href="#models" aria-label="Clear">×</a></span>' : "") +
      (P.monitored ? '<span class="chip">' + (P.monitored === "1" ? "With monitors" : "Without monitors") + ' <a href="#models" aria-label="Clear">×</a></span>' : "");
    p.appendChild(filters);
    var host = h("div");
    p.appendChild(host);
    page.appendChild(p);
    var shown = p.querySelector(".shown");
    function rows() {
      var v = filterValues(filters), q = (v.q || "").toLowerCase();
      keep({ q: v.q, status: v.status, owner: v.owner, schema: v.schema, sig: P.sig, sst: P.sst, monitored: P.monitored });
      return models.filter(function (m) {
        return (!q || m.short.toLowerCase().indexOf(q) >= 0) && (!v.status || m.st === v.status) &&
          (!v.owner || m.owner === v.owner) && (!v.schema || m.group === v.schema) &&
          (!P.sig || signalStatus(m, P.sig) === (P.sst || signalStatus(m, P.sig))) &&
          (!P.monitored || (P.monitored === "1") === (m.series.length > 0));
      });
    }
    var list = new List(host, {
      cols: [
        { label: "Model", w: "minmax(220px,2fr)", sort: function (m) { return m.short; } },
        { label: "Status", w: "96px", sort: function (m) { return RANK[m.st]; } },
        { label: "Owner", w: "minmax(100px,1fr)", sort: function (m) { return m.owner || "~"; } },
        { label: "Interval health", w: "160px" },
        { label: "Failing", w: "70px", num: true, sort: function (m) { return m.fails; } },
        { label: "Anomalies", w: "86px", num: true, sort: function (m) { return m.anoms; } },
        { label: "Latest", w: "100px", sort: function (m) { return m.last; } }
      ],
      sort: { i: 1, dir: 1 }, rows: rows(), min: 860, empty: "No models match these filters",
      href: function (m) { return "model?name=" + encodeURIComponent(m.name); },
      cells: function (m) {
        return [modelLink(m), statusPill(m.st), m.owner ? esc(m.owner) : '<span class="muted">-</span>',
          strip(m.healthDates, m.raw.h[1], false, 16), m.fails ? '<span class="strong">' + m.fails + "</span>" : '<span class="muted">0</span>',
          m.anoms ? '<span class="strong">' + m.anoms + "</span>" : '<span class="muted">0</span>', '<span class="mono">' + esc(day(m.last)) + "</span>"];
      },
      onCount: function (c) { shown.textContent = num(c) + " of " + num(models.length); }
    });
    bindFilters(filters, function () { list.set(rows()); });
  }

  // Model page ---------------------------------------------------------------

  function viewModel() {
    var m = models.find(function (x) { return x.name === route.params.name; }) ||
      models.find(function (x) { return x.short === route.params.name; });
    if (!m) { page.innerHTML = '<p class="muted">No model named ' + esc(route.params.name) + ' in this report. <a href="#models">All models</a></p>'; return; }
    var latestA = m.raw.a.filter(function (r) { return r.latest; });
    var version = (D.incidents.find(function (i) { return i.model === m.i; }) || {}).version;
    page.innerHTML = '<div class="crumbs"><a href="#models">Models</a> / <a href="#models?schema=' + encodeURIComponent(m.group) + '">' + esc(m.group) + "</a></div>" +
      '<div class="model-head"><h1>' + esc(m.leaf) + "</h1>" + statusPill(m.st) + "</div>" +
      '<ul class="facts"><li>Owner <b>' + esc(m.owner || "-") + "</b></li>" + (version ? "<li>Version <b class=\"mono\">" + esc(version) + "</b></li>" : "") +
      "<li>Latest interval <b>" + esc(day(m.last)) + "</b></li><li>Audits <b>" + latestA.length + "</b></li><li>Failing <b>" + m.fails +
      "</b></li><li>Anomalies <b>" + m.anoms + "</b></li></ul>";

    if (m.raw.h[1].length) {
      var hp = panel("Interval health", '<div class="end">' + plural(m.raw.h[1].length, "interval") + "</div>");
      hp.appendChild(h("div", { class: "panel-body" }, strip(m.healthDates, m.raw.h[1], true) + HEALTH_KEY));
      page.appendChild(hp);
    }
    var grid = h("div", { class: "grid two" });
    var cards = [];
    if (m.raw.r[1].length) {
      var rd = undelta(m.raw.r[0]), rv = m.raw.r[1];
      cards.push(chartCard("Run duration", "", function (host) {
        lineChart(host, { dates: rd, values: rv, k: 0 }, { label: "Run duration", unit: "ms" });
      }, function () { return rv.map(function (v, i) { return [esc(D.dates[rd[i]]), esc(duration(v))]; }).reverse(); }, ["Interval", "Duration"]));
    }
    m.series.forEach(function (s) { cards.push(seriesCard(s)); });
    if (cards.length === 1) grid.className = "grid";
    cards.forEach(function (c) { grid.appendChild(c); });
    if (cards.length) page.appendChild(grid);
    drawCharts(grid);

    var cp = panel("Columns", '<div class="end">' + plural(m.raw.c.length, "column") + "</div>");
    cp.appendChild(h("div", { class: "panel-body" }, m.raw.c.length ? '<div class="cols">' + m.raw.c.map(function (c) {
      return "<code>" + esc(c[0]) + "<span>" + esc(c[1]) + "</span></code>";
    }).join("") + "</div>" : '<span class="muted">No column types in SQLMesh state</span>'));
    page.appendChild(cp);

    if (m.raw.x.length) {
      var sp = panel("Schema changes");
      var sh = h("div");
      sp.appendChild(sh);
      page.appendChild(sp);
      schemaList(sh, m.raw.x.map(function (x) { return { m: m, x: x }; }));
    }

    var ap = panel("Audit history", '<div class="end">' + plural(m.raw.a.length, "result") + "</div>");
    var ah = h("div");
    ap.appendChild(ah);
    page.appendChild(ap);
    testList(ah, m.raw.a.map(function (r) { return { m: m, r: r }; }), true);
  }

  // Test results -------------------------------------------------------------

  function testList(host, rows, inModel, onCount) {
    var cols = [
      { label: "Status", w: "112px", sort: function (t) { return t.r[2] * 2 + (t.r[4] ? 0 : 1); } },
      { label: "Model", w: "minmax(200px,1.4fr)", sort: function (t) { return t.m.short; } },
      { label: "Audit", w: "minmax(200px,1.6fr)", sort: function (t) { return D.audits[t.r[1]][0]; } },
      { label: "Interval", w: "104px", sort: function (t) { return t.r[0]; } },
      { label: "Violations", w: "92px", num: true, sort: function (t) { return t.r[3] == null ? -1 : t.r[3]; } },
      { label: "Mode", w: "110px", sort: function (t) { return t.r[4]; } }
    ];
    if (inModel) cols.splice(1, 1);
    return new List(host, {
      cols: cols, rows: rows, sort: { i: inModel ? 2 : 3, dir: -1 }, min: inModel ? 640 : 860, empty: "No audit results match these filters",
      cells: function (t) {
        var kind = auditKind(t.r[2], t.r[4]);
        var c = [kindPill(kind), useLabel(t.r[1]), '<span class="mono">' + esc(day(t.r[0])) + "</span>",
          t.r[3] == null ? '<span class="muted">-</span>' : t.r[3] ? '<span class="strong">' + num(t.r[3]) + "</span>" : '<span class="muted">0</span>',
          t.r[4] ? "blocking" : '<span class="muted">non-blocking</span>'];
        if (!inModel) c.splice(1, 0, modelLink(t.m));
        return c;
      },
      expand: function (t) {
        var s = seriesFor(t.m, t.r[1]);
        var inc = t.r[5] >= 0 ? D.incidents[t.r[5]] : null;
        if (inc) return incidentDetail(inc, { series: s });
        if (s) return seriesDetail(t.m, s);
        var kind = auditKind(t.r[2], t.r[4]);
        return '<p class="muted" style="margin:0">' + (kind === "pass" ? "Passed with 0 violations" : "No stored detail") + " · " + modelLink(t.m) + "</p>";
      },
      onCount: onCount
    });
  }

  function viewTests() {
    var P = route.params;
    var p = panel("Test results", '<div class="end"><span class="shown"></span></div>');
    var filters = h("div", { class: "filters" });
    filters.innerHTML = '<input class="field" type="search" name="q" placeholder="Filter by model or audit" aria-label="Filter by model or audit" value="' + esc(P.q || "") + '">' +
      seg("status", [["", "All"], ["fail", "Blocking"], ["warn", "Non-blocking"], ["error", "Error"], ["pass", "Pass"], ["skip", "Skipped"]], P.status || "") +
      select("owner", "Owner", [["", "Any owner"]].concat(owners.map(function (o) { return [o, o]; })), P.owner) +
      select("schema", "Schema", [["", "Any schema"]].concat(schemas.map(function (s) { return [s, s]; })), P.schema) +
      seg("scope", [["", "Latest interval"], ["all", "All intervals"]], P.scope || "");
    p.appendChild(filters);
    var host = h("div");
    p.appendChild(host);
    page.appendChild(p);
    var shown = p.querySelector(".shown"), every = allTests();
    function rows() {
      var v = filterValues(filters), q = (v.q || "").toLowerCase();
      keep({ q: v.q, status: v.status, owner: v.owner, schema: v.schema, scope: v.scope });
      return every.filter(function (t) {
        if (v.scope !== "all" && !t.r.latest) return false;
        if (v.status && auditKind(t.r[2], t.r[4]) !== v.status) return false;
        if (v.owner && t.m.owner !== v.owner) return false;
        if (v.schema && t.m.group !== v.schema) return false;
        if (q && t.m.short.toLowerCase().indexOf(q) < 0 && D.audits[t.r[1]][0].toLowerCase().indexOf(q) < 0) return false;
        return true;
      });
    }
    var list = testList(host, rows(), false, function (c) { shown.textContent = num(c) + " shown"; });
    bindFilters(filters, function () { list.set(rows()); });
  }

  // Incidents ----------------------------------------------------------------

  function viewIncidents() {
    var P = route.params;
    var p = panel("Incidents", '<div class="end"><span class="shown"></span></div>');
    var filters = h("div", { class: "filters" });
    filters.innerHTML = '<input class="field" type="search" name="q" placeholder="Filter by model or audit" aria-label="Filter by model or audit" value="' + esc(P.q || "") + '">' +
      seg("status", [["", "All"], ["fail", "Blocking"], ["warn", "Non-blocking"], ["error", "Error"]], P.status || "") +
      select("owner", "Owner", [["", "Any owner"]].concat(owners.map(function (o) { return [o, o]; })), P.owner);
    p.appendChild(filters);
    var host = h("div");
    p.appendChild(host);
    page.appendChild(p);
    var shown = p.querySelector(".shown");
    var all = D.incidents.map(function (inc, i) {
      return { i: i, inc: inc, m: models[inc.model], kind: inc.status === "error" ? "error" : inc.blocking ? "fail" : "warn" };
    });
    function rows() {
      var v = filterValues(filters), q = (v.q || "").toLowerCase();
      keep({ q: v.q, status: v.status, owner: v.owner, i: P.i });
      return all.filter(function (r) {
        return (!v.status || r.kind === v.status) && (!v.owner || (r.m && r.m.owner === v.owner)) &&
          (!q || (r.m && r.m.short.toLowerCase().indexOf(q) >= 0) || D.audits[r.inc.use][0].toLowerCase().indexOf(q) >= 0);
      });
    }
    var list = new List(host, {
      cols: [
        { label: "Status", w: "112px", sort: function (r) { return r.kind; } },
        { label: "Model", w: "minmax(200px,1.4fr)", sort: function (r) { return r.m ? r.m.short : ""; } },
        { label: "Audit", w: "minmax(200px,1.6fr)", sort: function (r) { return D.audits[r.inc.use][0]; } },
        { label: "Interval", w: "104px", sort: function (r) { return r.inc.interval || ""; } },
        { label: "Violations", w: "92px", num: true, sort: function (r) { return r.inc.violations == null ? -1 : r.inc.violations; } },
        { label: "Started", w: "150px", sort: function (r) { return r.inc.started_at || ""; } }
      ],
      sort: { i: 5, dir: -1 }, rows: rows(), min: 860, empty: "No incidents in scope",
      cells: function (r) {
        return [kindPill(r.kind), r.m ? modelLink(r.m) : "-", useLabel(r.inc.use), '<span class="mono">' + esc(r.inc.interval || "-") + "</span>",
          r.inc.violations == null ? "-" : '<span class="strong">' + num(r.inc.violations) + "</span>", '<span class="mono">' + esc(r.inc.started_at || "-") + "</span>"];
      },
      expand: function (r) { return incidentDetail(r.inc, { series: r.m && seriesFor(r.m, r.inc.use) }); },
      onCount: function (c) { shown.textContent = num(c) + " shown"; }
    });
    bindFilters(filters, function () { list.set(rows()); });
    if (P.i != null && P.i !== "") list.open(function (r) { return String(r.i) === String(P.i); });
  }

  // Runs ---------------------------------------------------------------------

  function viewRuns() {
    var p = panel("Runs", '<div class="end">' + plural(D.runs.length, "run") + "</div>");
    var host = h("div");
    p.appendChild(host);
    page.appendChild(p);
    new List(host, {
      cols: [
        { label: "Status", w: "104px", sort: function (r) { return r.status || ""; } },
        { label: "Command", w: "90px", sort: function (r) { return r.command || ""; } },
        { label: "Environment", w: "minmax(100px,1fr)", sort: function (r) { return r.environment || ""; } },
        { label: "Started (UTC)", w: "160px", sort: function (r) { return r.started_at || ""; } },
        { label: "Duration", w: "96px", num: true, sort: function (r) { return r.duration_ms || 0; } },
        { label: "Model runs", w: "120px", num: true, sort: function (r) { return r.model_runs; } },
        { label: "Audits", w: "120px", num: true, sort: function (r) { return r.audits; } },
        { label: "Invocation", w: "120px" }
      ],
      sort: { i: 3, dir: -1 }, rows: D.runs, min: 900, empty: "No plans or runs recorded",
      cells: function (r) {
        var st = r.blocking_failures || r.status === "failure" || r.failed_runs ? "fail" : r.failing_audits || r.status === "unfinished" ? "warn" : "pass";
        return ['<span class="pill ' + st + '">' + esc(r.status || "unknown") + "</span>", "<strong>" + esc(r.command || "?") + "</strong>",
          esc(r.environment || "-"), '<span class="mono">' + esc(r.started_at || "-") + "</span>", esc(duration(r.duration_ms)),
          num(r.model_runs) + (r.failed_runs ? ' <span class="exp">' + r.failed_runs + " failed</span>" : ""),
          num(r.audits) + (r.failing_audits ? ' <span class="exp">' + r.failing_audits + " failing</span>" : ""),
          "<code>" + esc(r.id.slice(0, 12)) + "</code>"];
      }
    });
  }

  // Schema changes -----------------------------------------------------------

  function schemaList(host, rows) {
    return new List(host, {
      cols: [
        { label: "Recorded", w: "160px", sort: function (r) { return r.x[4] || ""; } },
        { label: "Model", w: "minmax(200px,1.4fr)", sort: function (r) { return r.m.short; } },
        { label: "Column", w: "minmax(120px,1fr)", sort: function (r) { return r.x[0]; } },
        { label: "Change", w: "110px", sort: function (r) { return r.x[1]; } },
        { label: "Old type", w: "minmax(90px,0.7fr)" },
        { label: "New type", w: "minmax(90px,0.7fr)" },
        { label: "Version", w: "110px" }
      ],
      sort: { i: 0, dir: -1 }, rows: rows, min: 900, empty: "No schema changes recorded",
      cells: function (r) {
        var kind = r.x[1] === "added" ? "warn" : "fail";
        return ['<span class="mono">' + esc(r.x[4] || "-") + "</span>", modelLink(r.m), "<code>" + esc(r.x[0]) + "</code>",
          '<span class="pill ' + kind + '">' + esc(r.x[1]) + "</span>", "<code>" + esc(r.x[2] || "-") + "</code>",
          "<code>" + esc(r.x[3] || "-") + "</code>", "<code>" + esc((r.x[5] || "-").slice(0, 12)) + "</code>"];
      }
    });
  }
  function viewSchema() {
    var rows = [];
    models.forEach(function (m) { m.raw.x.forEach(function (x) { rows.push({ m: m, x: x }); }); });
    var p = panel("Schema changes", '<div class="end">' + plural(rows.length, "change") + "</div>");
    var host = h("div");
    p.appendChild(host);
    page.appendChild(p);
    if (!rows.length && D.signals.schema == null) {
      host.innerHTML = '<div class="list-empty">Schema monitoring is off. Turn it on with <code>monitors={"schema": True}</code>.</div>';
      return;
    }
    schemaList(host, rows);
  }

  // ---------------------------------------------------------------- router

  var VIEWS = { overview: viewOverview, models: viewModels, model: viewModel, tests: viewTests, incidents: viewIncidents, runs: viewRuns, schema: viewSchema };
  function render() {
    route = parse();
    if (!VIEWS[route.view]) route = { view: "overview", params: {} };
    var tab = route.view === "model" ? "models" : route.view;
    app.querySelectorAll(".tabs a").forEach(function (a) {
      if (a.dataset.view === tab) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    layoutEl.classList.remove("wide");
    treeEl.removeAttribute("data-open");
    page.innerHTML = "";
    VIEWS[route.view]();
    drawTree();
    var title = { overview: "Dashboard", models: "Models", model: route.params.name, tests: "Test results", incidents: "Incidents", runs: "Runs", schema: "Schema changes" }[route.view];
    document.title = title + " · sqlmesh-observe · " + D.environment;
  }
  var lastHash = null;
  window.addEventListener("hashchange", function () {
    if (location.hash === lastHash) return;
    lastHash = location.hash;
    render();
    window.scrollTo(0, 0);
  });
  var resizeTimer = 0, lastWidth = innerWidth;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { if (Math.abs(innerWidth - lastWidth) > 40) { lastWidth = innerWidth; render(); } }, 200);
  });

  document.documentElement.setAttribute("data-ready", "");
  render();
  window.__report = { data: D, models: models, render: render };
})();
