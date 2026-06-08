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
  var state = { house: "activity", group: null, metrics: [], grain: "year",
                display: "grid", wiki: "", summary: false };

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
      { v: "inventory", label: t("ui.house.inventory") }
    ], function (v) {
      state.house = v; state.group = null; state.metrics = []; renderControls();
    })));

    // Dataset (group) within house
    var groups = groupsIn(state.house);
    var have = groups.some(function (g) { return g.id === state.group; });
    if ((!state.group || !have) && groups.length) {
      var prefer = state.house === "activity" ? "es_iabot_wayback" : "arcstat_links";
      var p = groups.filter(function (g) { return g.id === prefer; })[0];
      state.group = (p || groups[0]).id;
    }
    var sel = el("select", { on: { change: function (e) {
      state.group = e.target.value; state.metrics = []; renderControls();
    } } });
    groups.forEach(function (g) {
      var o = el("option", { value: g.id, text: groupLabel(g) });
      if (g.id === state.group) o.selected = true;
      sel.appendChild(o);
    });
    f.appendChild(field("ui.source", sel));

    // Tables (metrics) — multi-select
    var grp = curGroup();
    var mets = grp ? grp.metrics : [];
    if (!state.metrics.length && mets.length)
      state.metrics = mets.map(function (m) { return m.slug; });
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
    ], function (v) { state.grain = v; })));

    // Display
    f.appendChild(field("ui.display", radioGroup("display", state.display, [
      { v: "grid", label: t("ui.display.grid") },
      { v: "chart", label: t("ui.display.chart") }
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
  function rangeFor(grain) {
    var n = new Date();
    var iso = function (d) { return d.toISOString().slice(0, 10); };
    if (grain === "day") {
      return { from: iso(new Date(Date.UTC(n.getUTCFullYear(), n.getUTCMonth(), 1))),
               to:   iso(new Date(Date.UTC(n.getUTCFullYear(), n.getUTCMonth() + 1, 0))) };
    }
    if (grain === "month") {
      return { from: iso(new Date(Date.UTC(n.getUTCFullYear(), 0, 1))),
               to:   iso(new Date(Date.UTC(n.getUTCFullYear(), 11, 31))) };
    }
    return { from: null, to: null };   // year: all history
  }

  // --- run -----------------------------------------------------------------
  function run() {
    writeUrl();
    var result = document.getElementById("result");
    result.innerHTML = "";
    var status = document.getElementById("status");
    var grp = curGroup();
    if (!grp || !state.metrics.length) { status.textContent = ""; return; }
    status.textContent = t("ui.loading");

    var rg = rangeFor(state.grain);
    var qs = "&grain=" + state.grain +
      (rg.from ? "&from=" + rg.from : "") + (rg.to ? "&to=" + rg.to : "");

    var jobs = state.metrics.map(function (slug) {
      return api("/grid?source=" + encodeURIComponent(grp.source) +
        "&metric=" + encodeURIComponent(slug) + qs);
    });
    Promise.all(jobs).then(function (grids) {
      status.textContent = "";
      grids.forEach(function (g) {
        var block = el("div", { "class": "block" });
        block.appendChild(el("h2", {
          text: t("metric." + g.source + "." + g.metric, g.label || g.metric) }));
        var host = el("div", {});
        block.appendChild(host);
        if (state.display === "chart") renderChart(host, g);
        else renderGrid(host, g);
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
  function renderGrid(host, g) {
    host.innerHTML = "";
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
          applySort(g); renderGrid(host, g);
        } }, text: bucketLabel(g.buckets[i], g.grain) }));
    });
    table.appendChild(el("thead", {}, hr));

    var tb = el("tbody");
    function grandRow() {                  // the blue "All combined" total row
      var gr = dataRow(t("ui.combined"), g.all, isGauge, order);
      gr.className = "grand";
      return gr;
    }
    var showGrand = g.all && !filtering;   // only when not filtered to a single wiki
    if (showGrand && rows.length > 10) tb.appendChild(grandRow());  // top copy on long tables
    rows.forEach(function (r) { tb.appendChild(dataRow(r.entity, r.values, isGauge, order)); });
    if (showGrand) tb.appendChild(grandRow());                      // bottom copy (always)
    table.appendChild(tb);

    if (isGauge && g.metric.indexOf("uniq") === 0)
      host.appendChild(el("div", { "class": "caveat", text: t("ui.uniq_caveat") }));
    host.appendChild(el("div", { "class": "tablewrap" }, table));
  }

  // --- chart (uPlot): flow -> bars, gauge -> line; combined or one wiki -----
  function renderChart(host, g) {
    var isGauge = g.value_type === "gauge";
    var xs = g.buckets.map(function (b) { return Date.parse(b + "T00:00:00Z") / 1000; });
    var ser;
    if (state.wiki) {
      var row = g.rows.filter(function (r) { return wikiMatch(r.entity, state.wiki); })[0];
      ser = row ? row.values : g.buckets.map(function () { return null; });
    } else {
      ser = g.all || g.buckets.map(function () { return null; });
    }
    var w = host.clientWidth || (host.parentNode && host.parentNode.clientWidth) || 800;
    var opts = {
      width: w, height: 320,
      scales: { x: { time: true } },
      series: [{}, {
        label: g.unit || g.metric,
        stroke: "#36c",
        fill: isGauge ? "rgba(51,102,204,0.12)" : "rgba(51,102,204,0.55)",
        width: 2,
        paths: isGauge ? undefined : uPlot.paths.bars({ size: [0.6, 60] }),
        points: { show: isGauge }
      }],
    };
    var u = new uPlot(opts, [xs, ser], host);
    window.addEventListener("resize", function () {
      u.setSize({ width: host.clientWidth || w, height: 320 });
    });
  }

  function init() {
    document.getElementById("title").textContent = t("ui.title");
    document.getElementById("subtitle").textContent = t("ui.subtitle");
    renderLangBar();
    readUrl();
    api("/catalog").then(function (cat) {
      CATALOG = cat;
      renderControls();
      if (state.group && state.metrics.length) run();
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
