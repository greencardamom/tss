/* TSS dashboard. Data-driven from the read API (/catalog, /grid). All display
 * text comes from window.TSS.i18n via t(); numbers via the locale's formatter.
 * Vanilla JS, no build step. uPlot (vendored) draws the charts. */
(function () {
  "use strict";
  var TSS = window.TSS, I = TSS.i18n || {};
  var nf = new Intl.NumberFormat(TSS.lang || "en");

  function t(key, fallback) {
    return (I[key] != null) ? I[key] : (fallback != null ? fallback : key);
  }
  function fmt(v) { return (v == null) ? "" : nf.format(v); }
  function abbrev(n) {                    // compact axis ticks: 600k, 1.5M, 2B
    if (n == null) return "";
    var a = Math.abs(n);
    if (a >= 1e9) return +(n / 1e9).toFixed(1) + "B";
    if (a >= 1e6) return +(n / 1e6).toFixed(1) + "M";
    if (a >= 1e3) return +(n / 1e3).toFixed(0) + "k";
    return "" + n;
  }
  function el(tag, attrs) {
    var e = document.createElement(tag), a = attrs || {};
    for (var k in a) {
      if (k === "text") e.textContent = a[k];
      else if (k === "html") e.innerHTML = a[k];
      else if (k === "on") for (var ev in a[k]) e.addEventListener(ev, a[k][ev]);
      else if (a[k] != null) e.setAttribute(k, a[k]);
    }
    for (var i = 2; i < arguments.length; i++) {
      var c = arguments[i];
      if (c == null) continue;
      e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return e;
  }
  function api(path) {
    return fetch(TSS.apiBase + path).then(function (r) {
      if (!r.ok) throw new Error(path + " -> " + r.status);
      return r.json();
    });
  }
  function groupLabel(g) { return t(g.label_key, g.id); }
  function metLabel(srcSlug, m) {
    return t("metric." + srcSlug + "." + m.slug, m.label || m.slug);
  }
  // Forgiving wiki match: sources name wikis differently (eventstreams "en",
  // arcstat "en.wikipedia.org", numberofurl "en.wikipedia", iabotapi "enwiki").
  // "en" matches all of those; an exact name also matches.
  function wikiMatch(entity, w) {
    var e = entity.toLowerCase(), q = w.toLowerCase();
    return e === q || e === q + "wiki" || e.indexOf(q + ".") === 0;
  }

  // CATALOG = pulldown GROUPS from /catalog: {id, house, source, label_key, metrics}.
  var CATALOG = [];
  // offset = how many periods back from now (0 = current month/year). For day grain
  // it shifts months, for month grain it shifts years; year grain ignores it.
  var state = { house: "activity", group: null, metrics: [], grain: "year",
                display: "grid", wiki: "", summary: false, offset: 0 };

  // --- URL permalinks ------------------------------------------------------
  function readUrl() {
    var p = new URLSearchParams(location.search);
    if (p.get("house")) state.house = p.get("house");
    if (p.get("group")) state.group = p.get("group");
    if (p.get("metrics")) state.metrics = p.get("metrics").split(",").filter(Boolean);
    if (p.get("grain")) state.grain = p.get("grain");
    if (p.get("display")) state.display = p.get("display");
    if (p.get("wiki")) state.wiki = p.get("wiki");
    if (p.get("summary")) state.summary = p.get("summary") === "1";
    if (p.get("offset")) state.offset = parseInt(p.get("offset"), 10) || 0;
  }
  function writeUrl() {
    var p = new URLSearchParams();
    p.set("house", state.house);
    if (state.group) p.set("group", state.group);
    if (state.metrics.length) p.set("metrics", state.metrics.join(","));
    p.set("grain", state.grain);
    p.set("display", state.display);
    if (state.wiki) p.set("wiki", state.wiki);
    if (state.summary) p.set("summary", "1");
    if (state.offset) p.set("offset", state.offset);
    if (TSS.lang && TSS.lang !== "en") p.set("lang", TSS.lang);
    history.replaceState(null, "", location.pathname + "?" + p.toString());
  }

  function groupsIn(house) {
    return CATALOG.filter(function (g) { return g.house === house; });
  }
  function curGroup() {
    return CATALOG.filter(function (g) { return g.id === state.group; })[0];
  }

  // --- control panel -------------------------------------------------------
  function radioGroup(name, value, opts, onchange) {
    var wrap = el("span", { "class": "radios" });
    opts.forEach(function (o) {
      var id = name + "-" + o.v;
      var inp = el("input", { type: "radio", name: name, id: id, value: o.v });
      if (o.v === value) inp.checked = true;
      inp.addEventListener("change", function () { if (inp.checked) onchange(o.v); });
      wrap.appendChild(inp);
      wrap.appendChild(el("label", { "for": id, text: o.label }));
    });
    return wrap;
  }
  function field(labelKey, control) {
    return el("div", { "class": "field" },
      el("label", { "class": "flabel", text: t(labelKey) }), control);
  }

  function renderControls() {
    var f = document.getElementById("controls");
    f.innerHTML = "";

    // House
    f.appendChild(field("ui.house", radioGroup("house", state.house, [
      { v: "activity", label: t("ui.house.activity") },
      { v: "inventory", label: t("ui.house.inventory") },
      { v: "analysis", label: t("ui.house.analysis") }
    ], function (v) {
      state.house = v; state.group = null; state.metrics = []; state.offset = 0; renderControls();
    })));

    // Dataset (group) within house  (Analysis house: the list is curated questions)
    var groups = groupsIn(state.house);
    var have = groups.some(function (g) { return g.id === state.group; });
    if ((!state.group || !have) && groups.length) {
      var prefer = state.house === "activity" ? "iabotapi"
                 : state.house === "inventory" ? "arcstat_links" : null;
      var p = prefer && groups.filter(function (g) { return g.id === prefer; })[0];
      state.group = (p || groups[0]).id;
    }
    var dsel = el("select", { on: { change: function (e) {
      state.group = e.target.value; state.metrics = []; renderControls();
    } } });
    groups.forEach(function (g) {
      var o = el("option", { value: g.id, text: groupLabel(g) });
      if (g.id === state.group) o.selected = true;
      dsel.appendChild(o);
    });
    f.appendChild(field(state.house === "analysis" ? "ui.question" : "ui.source", dsel));

    if (state.house === "analysis") {        // analysis: just pick a question + Go
      f.appendChild(el("button", { "class": "go", on: { click: run } }, t("ui.go")));
      return;
    }

    // Tables (metrics) — multi-select
    var grp = curGroup();
    var mets = grp ? grp.metrics : [];
    var allSlugs = mets.map(function (m) { return m.slug; });
    // Always render in GROUP order: keep the current selection but reorder it to the
    // group's metric order (a stale ?metrics= permalink can be out of order). Fall
    // back to all metrics if the selection doesn't match this group.
    var sel = state.metrics.length
      ? allSlugs.filter(function (s) { return state.metrics.indexOf(s) >= 0; })
      : allSlugs.slice();
    state.metrics = sel.length ? sel : allSlugs.slice();
    if (mets.length > 1) {               // one-metric groups (e.g. each eventstreams table) need no TABLES picker
      var box = el("div", { "class": "checks" });
      mets.forEach(function (m) {
        var id = "m-" + m.slug;
        var inp = el("input", { type: "checkbox", id: id, value: m.slug });
        if (state.metrics.indexOf(m.slug) >= 0) inp.checked = true;
        inp.addEventListener("change", function () {
          state.metrics = Array.prototype.map.call(
            box.querySelectorAll("input:checked"), function (x) { return x.value; });
        });
        box.appendChild(el("span", { "class": "chk" }, inp,
          el("label", { "for": id, text: metLabel(grp ? grp.source : "", m) })));
      });
      f.appendChild(el("div", { "class": "field tables" },
        el("label", { "class": "flabel", text: t("ui.tables") }), box));
    }

    // Grain
    f.appendChild(field("ui.grain", radioGroup("grain", state.grain, [
      { v: "day", label: t("ui.grain.day") },
      { v: "month", label: t("ui.grain.month") },
      { v: "year", label: t("ui.grain.year") }
    ], function (v) { state.grain = v; state.offset = 0; renderControls(); })));

    // Period navigator: day -> pick month, month -> pick year, year -> all history.
    var pr = periodRange(state.grain, state.offset);
    var nav = el("span", { "class": "pnav" });
    if (state.grain === "year") {
      nav.appendChild(el("span", { "class": "plabel", text: pr.label }));
    } else {
      nav.appendChild(el("button", { type: "button", "class": "navbtn",
        on: { click: function () { state.offset -= 1; renderControls(); run(); } } }, "◀"));
      nav.appendChild(el("span", { "class": "plabel", text: pr.label }));
      var nextb = el("button", { type: "button", "class": "navbtn",
        on: { click: function () {
          if (state.offset < 0) { state.offset += 1; renderControls(); run(); }
        } } }, "▶");
      if (state.offset >= 0) nextb.disabled = true;   // never into the future
      nav.appendChild(nextb);
    }
    f.appendChild(field("ui.showing", nav));

    // Display
    f.appendChild(field("ui.display", radioGroup("display", state.display, [
      { v: "grid", label: t("ui.display.grid") },
      { v: "trend", label: t("ui.display.trend") },
      { v: "ranking", label: t("ui.display.ranking") }
    ], function (v) { state.display = v; })));

    // Wiki filter + summary
    var wikiInp = el("input", { type: "text", value: state.wiki,
      placeholder: t("ui.wiki.placeholder"), size: 22 });
    wikiInp.addEventListener("input", function () { state.wiki = wikiInp.value.trim(); });
    f.appendChild(field("ui.wiki", wikiInp));

    var sumInp = el("input", { type: "checkbox", id: "summary" });
    if (state.summary) sumInp.checked = true;
    sumInp.addEventListener("change", function () { state.summary = sumInp.checked; });
    f.appendChild(el("div", { "class": "field" },
      el("span", { "class": "chk" }, sumInp,
        el("label", { "for": "summary", text: t("ui.summary") }))));

    f.appendChild(el("button", { "class": "go", on: { click: run } }, t("ui.go")));
  }

  // --- date range defaults per grain ---------------------------------------
  // Each grain is scoped to its current CONTAINING period, so the Total column is
  // meaningful (not an arbitrary rolling-window sum):
  //   day   -> this calendar month  (Total = month-to-date)
  //   month -> this calendar year   (Total = year-to-date)
  //   year  -> all history          (Total = all-time)
  // offset shifts the window back: day grain -> by months, month grain -> by years
  // (0 = current). Returns {from, to, label} where label names the period shown.
  function periodRange(grain, offset) {
    var n = new Date();
    var iso = function (d) { return d.toISOString().slice(0, 10); };
    var loc = TSS.lang || "en";
    if (grain === "day") {                    // one calendar month
      var y = n.getUTCFullYear(), m = n.getUTCMonth() + (offset || 0);
      var from = new Date(Date.UTC(y, m, 1)), to = new Date(Date.UTC(y, m + 1, 0));
      return { from: iso(from), to: iso(to),
               label: from.toLocaleString(loc, { month: "long", year: "numeric",
                                                  timeZone: "UTC" }) };
    }
    if (grain === "month") {                  // one calendar year
      var yr = n.getUTCFullYear() + (offset || 0);
      return { from: yr + "-01-01", to: yr + "-12-31", label: String(yr) };
    }
    return { from: null, to: null, label: t("ui.all_time", "All history") };  // year
  }

  // --- run -----------------------------------------------------------------
  function run() {
    writeUrl();
    var result = document.getElementById("result");
    result.innerHTML = "";
    var status = document.getElementById("status");
    var grp = curGroup();
    if (!grp) { status.textContent = ""; return; }
    if (grp.house === "analysis") {          // curated cross-dataset question
      status.textContent = t("ui.loading");
      renderAnalysis(result, grp.analysis, status);
      return;
    }
    if (!state.metrics.length) { status.textContent = ""; return; }
    status.textContent = t("ui.loading");

    var rg = periodRange(state.grain, state.offset);
    var qs = "&grain=" + state.grain +
      (rg.from ? "&from=" + rg.from : "") + (rg.to ? "&to=" + rg.to : "");

    var jobs = state.metrics.map(function (slug) {
      return api("/grid?source=" + encodeURIComponent(grp.source) +
        "&metric=" + encodeURIComponent(slug) + qs);
    });
    Promise.all(jobs).then(function (grids) {
      status.textContent = "";
      // top-of-results description: group-specific if defined, else source-level
      var srcDesc = t("desc.group." + grp.id, t("desc." + grp.source, ""));
      if (srcDesc) result.appendChild(el("div", { "class": "caption source-caption", html: srcDesc }));
      grids.forEach(function (g) {
        var block = el("div", { "class": "block" });
        var head = el("h2", {}, el("span", { text:
          t("metric." + g.source + "." + g.metric, g.label || g.metric) }));
        block.appendChild(head);
        // per-table caption (trusted HTML) — between the title and the table
        var desc = t("desc." + g.source + "." + g.metric, "");
        if (desc) block.appendChild(el("div", { "class": "caption", html: desc }));
        var host = el("div", {});
        block.appendChild(host);
        if (state.display === "trend") renderTrend(host, g);
        else if (state.display === "ranking") renderRanking(host, g);
        else renderGrid(host, g, head);     // head: where the Show all/Collapse btn sits
        result.appendChild(block);
      });
    }).catch(function (e) { status.textContent = "" + e; });
  }

  // --- grid ----------------------------------------------------------------
  function bucketLabel(b, grain) {
    if (grain === "year") return b.slice(0, 4);
    if (grain === "month") return b.slice(0, 7);
    return b;
  }
  function rowTotal(vals) {
    var s = 0, any = false;
    vals.forEach(function (v) { if (v != null) { s += v; any = true; } });
    return any ? s : null;
  }
  function lastVal(vals) {
    for (var i = vals.length - 1; i >= 0; i--) if (vals[i] != null) return vals[i];
    return null;
  }
  function cell(v) {
    if (v == null) return el("td", { "class": "num nodata", title: t("ui.no_data") }, "·");
    return el("td", { "class": "num" }, fmt(v));
  }
  function dataRow(name, vals, isGauge, order) {
    var tr = el("tr");
    tr.appendChild(el("th", { "class": "site", text: name }));
    tr.appendChild(el("td", { "class": "num total" },   // total/latest FIRST
      fmt(isGauge ? lastVal(vals) : rowTotal(vals))));
    order.forEach(function (i) { tr.appendChild(cell(vals[i])); });
    return tr;
  }
  function applySort(g) {
    if (!g._sort) return;
    var c = g._sort.col, d = g._sort.dir;
    g.rows.sort(function (a, b) {
      var av = a.values[c], bv = b.values[c];
      av = (av == null) ? -Infinity : av; bv = (bv == null) ? -Infinity : bv;
      return (av - bv) * d;
    });
  }
  function renderGrid(host, g, head) {
    host.innerHTML = "";
    if (head) {                            // drop a stale expand/collapse btn (re-render)
      var ob = head.querySelector("button.expand");
      if (ob) ob.remove();
    }
    var isGauge = g.value_type === "gauge";
    var filtering = !!state.wiki;
    var rows = filtering
      ? g.rows.filter(function (r) { return wikiMatch(r.entity, state.wiki); })
      : (state.summary ? [] : g.rows);

    if (filtering && rows.length === 0) {
      host.appendChild(el("div", { "class": "muted", text: t("ui.no_wiki_match") + " " + state.wiki }));
      return;
    }

    // Column order: Total/Latest first, then periods NEWEST -> oldest.
    var order = g.buckets.map(function (_b, i) { return i; }).reverse();
    var table = el("table", { "class": "grid" });
    var hr = el("tr");
    hr.appendChild(el("th", { "class": "site", text: t("ui.site") }));
    hr.appendChild(el("th", { "class": "num total",
      text: isGauge ? t("ui.latest", "Latest") : t("ui.total") }));
    order.forEach(function (i) {
      hr.appendChild(el("th", { "class": "num sortable",
        on: { click: function () {
          g._sort = { col: i, dir: (g._sort && g._sort.col === i && g._sort.dir === -1) ? 1 : -1 };
          applySort(g); renderGrid(host, g, head);
        } }, text: bucketLabel(g.buckets[i], g.grain) }));
    });
    table.appendChild(el("thead", {}, hr));

    var LIMIT = 6;                         // collapse long tables to this many rows
    var longTable = rows.length > LIMIT;
    var expanded = !!g._expanded;
    var shown = (longTable && !expanded) ? rows.slice(0, LIMIT) : rows;

    var tb = el("tbody");
    function grandRow() {                  // the blue "All combined" total row
      var gr = dataRow(t("ui.combined"), g.all, isGauge, order);
      gr.className = "grand";
      return gr;
    }
    var collapsed = longTable && !expanded;
    var showGrand = g.all && !filtering;   // only when not filtered to a single wiki
    if (showGrand && longTable) tb.appendChild(grandRow());   // TOP on long tables
    shown.forEach(function (r) { tb.appendChild(dataRow(r.entity, r.values, isGauge, order)); });
    if (showGrand && !collapsed) tb.appendChild(grandRow());  // BOTTOM only when not collapsed
                                                              // (short table, or expanded)
    table.appendChild(tb);

    if (isGauge && g.metric.indexOf("uniq") === 0)
      host.appendChild(el("div", { "class": "caveat", text: t("ui.uniq_caveat") }));
    host.appendChild(el("div", { "class": "tablewrap" }, table));
    if (longTable) {
      var btn = el("button", { "class": "expand", on: { click: function () {
        g._expanded = !expanded; renderGrid(host, g, head);
      } } }, expanded
        ? t("ui.collapse")
        : t("ui.expand") + " (" + fmt(rows.length) + ")");
      (head || host).appendChild(btn);     // sits next to the table title
    }
  }

  // --- charts --------------------------------------------------------------
  var PALETTE = ["#3366cc", "#dc3912", "#109618", "#ff9900", "#990099",
                 "#0099c6", "#dd4477", "#66aa00", "#b82e2e", "#316395"];

  // Rank rows by total (flow) / latest reading (gauge); drop empties; top n.
  function topRows(rows, valueType, n) {
    var agg = (valueType === "gauge") ? lastVal : rowTotal;
    return rows.map(function (r) {
        return { entity: r.entity, values: r.values, v: agg(r.values) };
      })
      .filter(function (r) { return r.v != null; })
      .sort(function (a, b) { return (b.v || 0) - (a.v || 0); })
      .slice(0, n);
  }

  // #1 Trend: a line per series over time — top-N wikis, or the one filtered
  // wiki, or the combined total (summary-only). Chronological x.
  function renderTrend(host, g) {
    var xs = g.buckets.map(function (b) { return Date.parse(b + "T00:00:00Z") / 1000; });
    var lines = [];
    if (state.wiki) {
      var r = g.rows.filter(function (x) { return wikiMatch(x.entity, state.wiki); })[0];
      if (r) lines.push({ label: r.entity, values: r.values });
    } else if (state.summary && g.all) {
      lines.push({ label: t("ui.combined"), values: g.all });
    } else {
      topRows(g.rows, g.value_type, 8).forEach(function (r) {
        lines.push({ label: r.entity, values: r.values });
      });
    }
    if (!lines.length && g.all) lines.push({ label: t("ui.combined"), values: g.all });

    var data = [xs].concat(lines.map(function (l) { return l.values; }));
    var series = [{}].concat(lines.map(function (l, i) {
      return { label: l.label, stroke: PALETTE[i % PALETTE.length], width: 1.6,
               points: { show: true, size: 4 } };
    }));
    var w = host.clientWidth || (host.parentNode && host.parentNode.clientWidth) || 800;
    var u = new uPlot({ width: w, height: 340, scales: { x: { time: true } },
                       series: series,
                       axes: [ {}, { size: 60,   // wider gutter; abbreviated ticks (no clipping)
                                     values: function (u, vals) { return vals.map(abbrev); } } ] },
                      data, host);
    window.addEventListener("resize", function () {
      u.setSize({ width: host.clientWidth || w, height: 340 });
    });
  }

  // #2 Ranking: top-N wikis for the shown period as horizontal bars (leaderboard).
  function renderRanking(host, g) {
    var base = state.wiki
      ? g.rows.filter(function (r) { return wikiMatch(r.entity, state.wiki); })
      : g.rows;
    var top = topRows(base, g.value_type, 20);
    if (!top.length) {
      host.appendChild(el("div", { "class": "muted", text: t("ui.no_data") }));
      return;
    }
    var max = top[0].v || 1;
    var wrap = el("div", { "class": "ranking" });
    top.forEach(function (r, i) {
      var pct = Math.max(1, (r.v / max) * 100);
      wrap.appendChild(el("div", { "class": "rankrow" },
        el("span", { "class": "rankname", title: r.entity, text: r.entity }),
        el("span", { "class": "rankbar" }, el("span", { "class": "rankfill",
          style: "width:" + pct + "%;background:" + PALETTE[i % PALETTE.length] })),
        el("span", { "class": "rankval", text: fmt(r.v) })));
    });
    host.appendChild(wrap);
  }

  // --- Analysis: a curated question, its series shown side-by-side -----------
  function renderAnalysis(result, q, status) {
    if (!q) { status.textContent = ""; return; }
    // MONTHLY grain so the chart gets 12 points a year instead of 1 and the
    // current year is visible while it happens -- but only where that is also
    // CORRECT. A question whose series are all plain flows (work done per period)
    // sums cleanly by month. Ratio and delta series ride on gauge sources, where
    // a bucket is the sum of each wiki's *last reading*: the running month is
    // still half-collected across ~850 wikis, so its total dips and any
    // month-over-month arithmetic on it is nonsense. Those questions stay yearly,
    // exactly as before.
    var monthly = q.series.every(function (s) { return !s.op; });
    var KL = monthly ? 7 : 4;               // bucket key length: YYYY-MM vs YYYY
    // No `to`: an end date must be computed or omitted, never hard-coded, or the
    // view silently truncates the moment the year rolls over.
    var qs = "&entity=_all&grain=" + (monthly ? "month" : "year") + "&from=2015-01-01";
    var pct = (q.unit === "percent");   // ratio cards render as % (level), not sums
    var jobs = [];
    function fetchPart(p, label, role) {
      jobs.push(api("/series?source=" + encodeURIComponent(p[0]) +
        "&metric=" + encodeURIComponent(p[1]) + qs)
        .then(function (d) { d.__label = label; d.__role = role; return d; }));
    }
    q.series.forEach(function (s) {
      if (s.op === "ratio") {
        (s.num || []).forEach(function (p) { fetchPart(p, s.label, "num"); });
        (s.den || []).forEach(function (p) { fetchPart(p, s.label, "den"); });
      } else {
        (s.parts || []).forEach(function (p) { fetchPart(p, s.label, "sum"); });
      }
    });
    Promise.all(jobs).then(function (ds) {
      status.textContent = "";
      var num = {}, den = {}, sum = {}, keyset = {};
      q.series.forEach(function (s) { num[s.label] = {}; den[s.label] = {}; sum[s.label] = {}; });
      ds.forEach(function (d) {
        (d.points || []).forEach(function (pt) {
          var k = pt.bucket.slice(0, KL); keyset[k] = 1;   // "YYYY-MM" or "YYYY"
          var bag = d.__role === "num" ? num : d.__role === "den" ? den : sum;
          bag[d.__label][k] = (bag[d.__label][k] || 0) + pt.value;
        });
      });
      var keys = Object.keys(keyset).sort();
      var cur = String(new Date().getUTCFullYear());

      // per[label][k]: value to plot (ratio % for ratio series, summed flow otherwise).
      // cnt[label][k]: the raw numerator behind a ratio (shown in the Count column).
      var per = {}, cnt = {};
      q.series.forEach(function (s) {
        per[s.label] = {}; cnt[s.label] = {};
        if (s.op === "delta") {
          // year-over-year change of a gauge: this year's level minus the
          // previous year that has data (first data-year has no delta).
          var sm = sum[s.label];
          var ks = keys.filter(function (k) { return k in sm; });
          ks.forEach(function (k, i) { if (i > 0) per[s.label][k] = sm[k] - sm[ks[i - 1]]; });
        } else {
          keys.forEach(function (k) {
            if (s.op === "ratio") {
              var dv = den[s.label][k];
              if (dv) {
                var nv = (num[s.label][k] || 0) + (s.num_const || 0);
                per[s.label][k] = nv / dv * (s.scale || 100);
                cnt[s.label][k] = nv;
              }
            } else if (k in sum[s.label]) {
              per[s.label][k] = sum[s.label][k];
            }
          });
        }
      });
      function latest(m) {                 // most recent bucket that has a value
        for (var i = keys.length - 1; i >= 0; i--) if (m[keys[i]] != null) return keys[i];
        return null;
      }
      // The table's columns are calendar years. On a monthly question the values
      // fold back up (flows sum cleanly); on a yearly one the keys are already
      // years and this is a pass-through.
      function byYear(m) {
        var out = {};
        for (var k in m) { var y = k.slice(0, 4); out[y] = (out[y] || 0) + m[k]; }
        return out;
      }

      // Opt-in combined total ("combined": true), shown as a table row and a
      // trend line. Only the plain flow series are summed — ratio/delta series
      // are not additive, so they stay out of it. comb stays null when off.
      var flows = pct ? [] : q.series.filter(function (s) { return !s.op; });
      var comb = null;
      if (q.combined && flows.length > 1) {
        comb = {};
        flows.forEach(function (s) {
          for (var m2 in per[s.label]) comb[m2] = (comb[m2] || 0) + per[s.label][m2];
        });
      }

      var block = el("div", { "class": "block" });
      block.appendChild(el("h2", {}, el("span", { text: t(q.q, q.id) })));
      var note = t(q.note, "");
      if (note) block.appendChild(el("div", { "class": "caption", html: note }));

      // side-by-side summary table. percent cards: series | latest % | count.
      // flow cards: series | this year | all time.
      var table = el("table", { "class": "grid" });
      var hr = el("tr");
      hr.appendChild(el("th", { "class": "site", text: t("ui.tool") }));
      hr.appendChild(el("th", { "class": "num", text: pct ? t("ui.latest")
                                                          : t("ui.this_year") + " (" + cur + ")" }));
      hr.appendChild(el("th", { "class": "num total", text: pct ? t("ui.count") : t("ui.alltime") }));
      table.appendChild(el("thead", {}, hr));
      var tb = el("tbody");
      q.series.forEach(function (s) {
        var tr = el("tr");
        tr.appendChild(el("th", { "class": "site", text: s.label }));
        if (pct) {
          var ly = latest(per[s.label]);
          tr.appendChild(el("td", { "class": "num",
            text: ly != null ? (Math.round(per[s.label][ly] * 10) / 10) + "%" : "·" }));
          tr.appendChild(el("td", { "class": "num total" },
            fmt(ly != null && cnt[s.label][ly] != null ? Math.round(cnt[s.label][ly]) : null)));
        } else {
          // Yearly, and deliberately INCLUDING the current part-month, so the
          // all-time figure stays a live running total.
          var m = byYear(per[s.label]), allt = 0, any = false;
          for (var y in m) { allt += m[y]; any = true; }
          tr.appendChild(cell(m[cur] != null ? m[cur] : null));
          tr.appendChild(el("td", { "class": "num total" }, fmt(any ? allt : null)));
        }
        tb.appendChild(tr);
      });
      if (comb) {
        var cby = byYear(comb), callt = 0, cany = false;
        for (var cy in cby) { callt += cby[cy]; cany = true; }
        var gtr = el("tr", { "class": "grand" });
        gtr.appendChild(el("th", { "class": "site", text: t("ui.combined") }));
        gtr.appendChild(cell(cby[cur] != null ? cby[cur] : null));
        gtr.appendChild(el("td", { "class": "num total" }, fmt(cany ? callt : null)));
        tb.appendChild(gtr);
      }
      table.appendChild(tb);
      block.appendChild(el("div", { "class": "tablewrap" }, table));

      // trend: one line per series over time, plus a dashed combined line last
      // (dashed + neutral so it reads as a derived total, not another tool).
      var host = el("div", {});
      block.appendChild(host);
      // Monthly questions stop at the last COMPLETE month: the running month is
      // still filling, so plotting it would drag the right-hand edge down every
      // month. Yearly questions keep every bucket, as before.
      var xk = keys;
      if (monthly) {
        var n2 = new Date();
        var cutoff = new Date(Date.UTC(n2.getUTCFullYear(), n2.getUTCMonth() - 1, 1))
                       .toISOString().slice(0, 7);
        xk = keys.filter(function (k) { return k <= cutoff; });
      }
      var xs = xk.map(function (k) {
        return Date.parse(k + (monthly ? "-01" : "-01-01") + "T00:00:00Z") / 1000;
      });
      var data = [xs], series = [{}];
      // On a monthly line, a gap INSIDE a series' active span means the tool
      // simply did not run that month, so it plots as 0 and the line stays
      // unbroken. Gaps before it started (or after it stopped) stay null — that
      // is absence of data, not a month of no work.
      function line(m) {
        var first = null, last = null;
        xk.forEach(function (k) { if (m[k] != null) { if (first == null) first = k; last = k; } });
        return xk.map(function (k) {
          if (m[k] != null) return m[k];
          return (monthly && first != null && k > first && k < last) ? 0 : null;
        });
      }
      // Points off on monthly: ~120 dots on an 800px canvas smear into a blob.
      var pt = { show: !monthly, size: 4 };
      q.series.forEach(function (s, i) {
        data.push(line(per[s.label]));
        series.push({ label: s.label, stroke: PALETTE[i % PALETTE.length], width: 1.8,
                      points: pt });
      });
      if (comb) {
        data.push(line(comb));
        series.push({ label: t("ui.combined"), stroke: "#555", width: 2.2, dash: [6, 3],
                      points: pt });
      }
      var w = host.clientWidth || result.clientWidth || 800;
      var u = new uPlot({ width: w, height: 300, scales: { x: { time: true } }, series: series,
                          axes: [ {}, { size: 60, values: function (u, v) {
                            return v.map(pct ? function (x) { return x + "%"; } : abbrev); } } ] },
                        data, host);
      window.addEventListener("resize", function () {
        u.setSize({ width: host.clientWidth || w, height: 300 });
      });
      result.appendChild(block);
    }).catch(function (e) { status.textContent = "" + e; });
  }

  function init() {
    document.getElementById("title").textContent = t("ui.title");
    document.getElementById("subtitle").textContent = t("ui.subtitle");
    renderLangBar();
    readUrl();
    api("/catalog").then(function (cat) {
      CATALOG = cat;
      // append the curated Analysis-house questions as pseudo-groups
      (TSS.analysis || []).forEach(function (q) {
        CATALOG.push({ id: q.id, house: "analysis", source: null,
                       label_key: q.q, analysis: q });
      });
      renderControls();
      var g = curGroup();
      if (state.group && (state.metrics.length || (g && g.house === "analysis"))) run();
    }).catch(function (e) {
      document.getElementById("status").textContent = "" + e;
    });
  }

  function renderLangBar() {
    var bar = document.getElementById("langbar");
    bar.innerHTML = "";
    bar.appendChild(el("span", { "class": "muted", text: t("ui.language") + ": " }));
    (TSS.langs || ["en"]).forEach(function (lg, i) {
      if (i) bar.appendChild(document.createTextNode(" · "));
      if (lg === TSS.lang) bar.appendChild(el("strong", { text: lg }));
      else {
        var p = new URLSearchParams(location.search); p.set("lang", lg);
        bar.appendChild(el("a", { href: location.pathname + "?" + p.toString(), text: lg }));
      }
    });
  }

  init();
})();
