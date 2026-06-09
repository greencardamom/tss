"""Public (no-auth) read endpoints: catalog, series, drill-through."""
import json
import os

from flask import Blueprint, request, jsonify

from db import get_db

bp = Blueprint("read", __name__)

# Optional pulldown groupings (config, no code): a source may be split into named
# metric subsets; unlisted sources default to one group = the whole source.
_GROUPS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "groups.json")
_groups_cache = None


def _groups_raw():
    global _groups_cache
    if _groups_cache is None:
        try:
            with open(_GROUPS_PATH, encoding="utf-8") as fh:
                _groups_cache = json.load(fh)
        except (OSError, ValueError):
            _groups_cache = {}
    return _groups_cache


def _group_overrides():
    return {k: v for k, v in _groups_raw().items() if not k.startswith("_")}


def _group_order():
    # "_order": group ids in the order the pulldown should list them
    return _groups_raw().get("_order", [])


def _filter_metrics(metrics, gdef):
    explicit = gdef.get("metrics")
    if explicit is not None:                       # exactly these slugs, in this order
        order = {s: i for i, s in enumerate(explicit)}
        sub = [m for m in metrics if m["slug"] in order]
        sub.sort(key=lambda m: order[m["slug"]])
        return sub
    cats = gdef.get("categories")
    excl = set(gdef.get("exclude_categories") or [])
    inc = set(gdef.get("include_metrics") or [])
    out = []
    for m in metrics:
        c = m.get("category")
        if cats is not None:
            if c in cats or m["slug"] in inc:
                out.append(m)
        elif c not in excl or m["slug"] in inc:
            out.append(m)
    return out


def _num(v):
    """Render a DECIMAL as int when integral, else float (JSON-friendly)."""
    f = float(v)
    return int(f) if f.is_integer() else f


# --- catalog ---------------------------------------------------------------

@bp.get("/sources")
def list_sources():
    cur = get_db().cursor()
    cur.execute(
        "SELECT slug, name, description FROM source WHERE is_active=1 ORDER BY slug"
    )
    return jsonify(cur.fetchall())


@bp.get("/sources/<slug>/metrics")
def list_metrics(slug):
    cur = get_db().cursor()
    cur.execute(
        "SELECT m.slug, m.label, m.unit, m.value_type, m.default_agg, m.category "
        "FROM metric m JOIN source s ON s.source_id = m.source_id "
        "WHERE s.slug = %s ORDER BY m.category, m.slug",
        (slug,),
    )
    return jsonify(cur.fetchall())


@bp.get("/sources/<slug>/entities")
def list_entities(slug):
    metric = request.args.get("metric")
    sql = (
        "SELECT DISTINCT r.entity FROM rollup r "
        "JOIN source s ON s.source_id = r.source_id "
        "JOIN metric m ON m.metric_id = r.metric_id "
        "WHERE s.slug = %s AND r.entity <> '' "
    )
    params = [slug]
    if metric:
        sql += "AND m.slug = %s "
        params.append(metric)
    sql += "ORDER BY r.entity"
    cur = get_db().cursor()
    cur.execute(sql, params)
    return jsonify([r["entity"] for r in cur.fetchall()])


# --- series (the graph engine) --------------------------------------------

@bp.get("/series")
def series():
    source = request.args.get("source")
    metric = request.args.get("metric")
    entity = request.args.get("entity", "_all")
    grain = request.args.get("grain", "day")
    frm = request.args.get("from")
    to = request.args.get("to")

    if not (source and metric):
        return jsonify(error="source and metric are required"), 400
    if grain not in ("day", "month", "year"):
        return jsonify(error="grain must be day|month|year"), 400

    ent = "" if entity in ("_all", "") else entity

    cur = get_db().cursor()
    cur.execute(
        "SELECT m.metric_id, m.label, m.unit, s.source_id "
        "FROM metric m JOIN source s ON s.source_id = m.source_id "
        "WHERE s.slug = %s AND m.slug = %s",
        (source, metric),
    )
    meta = cur.fetchone()
    if not meta:
        return jsonify(error="unknown source/metric"), 404

    sql = (
        "SELECT bucket, value, samples FROM rollup "
        "WHERE source_id=%s AND metric_id=%s AND entity=%s AND grain=%s "
    )
    params = [meta["source_id"], meta["metric_id"], ent, grain]
    if frm:
        sql += "AND bucket >= %s "
        params.append(frm)
    if to:
        sql += "AND bucket <= %s "
        params.append(to)
    sql += "ORDER BY bucket"
    cur.execute(sql, params)

    points = [
        {"bucket": r["bucket"].isoformat(), "value": _num(r["value"]), "samples": r["samples"]}
        for r in cur.fetchall()
    ]
    return jsonify(
        source=source,
        metric={"slug": metric, "label": meta["label"], "unit": meta["unit"]},
        entity=entity,
        grain=grain,
        points=points,
    )


# --- drill-through (raw events behind a bucket) ----------------------------

@bp.get("/events")
def events():
    source = request.args.get("source")
    metric = request.args.get("metric")
    entity = request.args.get("entity")
    date = request.args.get("date")
    page = max(int(request.args.get("page", 1)), 1)
    limit = min(int(request.args.get("limit", 100)), 1000)
    offset = (page - 1) * limit

    if not (source and metric and date):
        return jsonify(error="source, metric and date are required"), 400

    cur = get_db().cursor()
    cur.execute(
        "SELECT m.metric_id, s.source_id, s.ref_url_tpl "
        "FROM metric m JOIN source s ON s.source_id = m.source_id "
        "WHERE s.slug = %s AND m.slug = %s",
        (source, metric),
    )
    meta = cur.fetchone()
    if not meta:
        return jsonify(error="unknown source/metric"), 404

    where = "source_id=%s AND metric_id=%s AND ts=%s "
    params = [meta["source_id"], meta["metric_id"], date]
    if entity:
        where += "AND entity=%s "
        params.append(entity)

    cur.execute(f"SELECT COUNT(*) AS c FROM event WHERE {where}", params)
    total = cur.fetchone()["c"]

    cur.execute(
        f"SELECT entity, ts, value, ref_id, dims FROM event WHERE {where} "
        "ORDER BY event_id LIMIT %s OFFSET %s",
        params + [limit, offset],
    )
    tpl = meta["ref_url_tpl"]
    rows = []
    for r in cur.fetchall():
        ref_url = None
        if tpl and r["ref_id"]:
            ref_url = tpl.replace("{entity}", r["entity"] or "").replace(
                "{ref_id}", str(r["ref_id"])
            )
        dims = r["dims"]
        if isinstance(dims, str):
            try:
                dims = json.loads(dims)
            except (ValueError, TypeError):
                pass
        rows.append(
            {
                "ts": r["ts"].isoformat(),
                "entity": r["entity"],
                "value": _num(r["value"]),
                "ref_id": r["ref_id"],
                "ref_url": ref_url,
                "dims": dims,
            }
        )

    # total == 0 may mean a genuine zero day OR that the raw events for an old date
    # have been archived to Parquet and pruned. The rollup still has the count.
    return jsonify(total=total, page=page, limit=limit, events=rows)


# --- front-end helpers -----------------------------------------------------

@bp.get("/catalog")
def catalog():
    """One call that powers the dashboard control panel: the pulldown GROUPS,
    each with its metrics and a derived `house` (flow vs gauge). house='inventory'
    when ALL of the source's metrics are gauges (levels), else 'activity'. A group
    is normally one source; groups.json may split a source into metric subsets
    (e.g. arcstat -> archive links + media). Each group: {id, house, source,
    label_key (for i18n; falls back to source.<slug>), metrics}."""
    cur = get_db().cursor()
    cur.execute("SELECT source_id, slug, name, description FROM source "
                "WHERE is_active=1 ORDER BY slug")
    sources = cur.fetchall()
    cur.execute("SELECT source_id, slug, label, unit, value_type, default_agg, "
                "category FROM metric ORDER BY category, slug")
    by_src = {}
    for m in cur.fetchall():
        by_src.setdefault(m["source_id"], []).append(
            {k: m[k] for k in ("slug", "label", "unit", "value_type",
                               "default_agg", "category")})
    overrides = _group_overrides()
    out = []
    for s in sources:
        ms = by_src.get(s["source_id"], [])
        house = "inventory" if ms and all(m["value_type"] == "gauge"
                                          for m in ms) else "activity"
        defs = overrides.get(s["slug"])
        if defs:
            for d in defs:
                out.append({"id": d["id"], "house": house, "source": s["slug"],
                            "label_key": d.get("label_key", "source." + s["slug"]),
                            "metrics": _filter_metrics(ms, d)})
        else:
            out.append({"id": s["slug"], "house": house, "source": s["slug"],
                        "label_key": "source." + s["slug"], "metrics": ms})
    order = _group_order()
    if order:                              # explicit pulldown order; unlisted go last
        rank = {gid: i for i, gid in enumerate(order)}
        out.sort(key=lambda g: rank.get(g["id"], len(rank)))
    return jsonify(out)


@bp.get("/grid")
def grid():
    """All entities x periods for one source+metric, in one response -- the
    spreadsheet grid (per-wiki rows x time columns) the dashboard draws. `/series`
    is per-entity; this is the matrix. Values are None where a bucket has no data
    (a real gap), distinct from a real 0. The combined `_all` row is returned
    separately. Client computes totals (sum for flow; for gauges a period-sum is
    meaningless, so the client hides the total column)."""
    source = request.args.get("source")
    metric = request.args.get("metric")
    grain = request.args.get("grain", "year")
    frm = request.args.get("from")
    to = request.args.get("to")
    if not (source and metric):
        return jsonify(error="source and metric are required"), 400
    if grain not in ("day", "month", "year"):
        return jsonify(error="grain must be day|month|year"), 400

    cur = get_db().cursor()
    cur.execute(
        "SELECT m.metric_id, m.label, m.unit, m.value_type, m.default_agg, "
        "s.source_id FROM metric m JOIN source s ON s.source_id = m.source_id "
        "WHERE s.slug = %s AND m.slug = %s", (source, metric))
    meta = cur.fetchone()
    if not meta:
        return jsonify(error="unknown source/metric"), 404

    sql = ("SELECT entity, bucket, value FROM rollup "
           "WHERE source_id=%s AND metric_id=%s AND grain=%s ")
    params = [meta["source_id"], meta["metric_id"], grain]
    if frm:
        sql += "AND bucket >= %s "
        params.append(frm)
    if to:
        sql += "AND bucket <= %s "
        params.append(to)
    cur.execute(sql, params)

    ent, allrow, bset = {}, {}, set()
    for r in cur.fetchall():
        b = r["bucket"].isoformat()
        bset.add(b)
        (allrow if r["entity"] == "" else ent.setdefault(r["entity"], {}))[b] = \
            _num(r["value"])
    buckets = sorted(bset)

    def vals(d):
        return [d.get(b) for b in buckets]   # None = no data for that bucket

    return jsonify(
        source=source, metric=metric, grain=grain,
        value_type=meta["value_type"], agg=meta["default_agg"],
        label=meta["label"], unit=meta["unit"],
        buckets=buckets,
        rows=[{"entity": e, "values": vals(ent[e])} for e in sorted(ent)],
        all=(vals(allrow) if allrow else None),
    )
