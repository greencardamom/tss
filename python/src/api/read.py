"""Public (no-auth) read endpoints: catalog, series, drill-through."""
import json

from flask import Blueprint, request, jsonify

from db import get_db

bp = Blueprint("read", __name__)


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
    """One call that powers the dashboard control panel: every source with its
    metrics, plus a derived `house` (flow vs gauge). house='inventory' when ALL
    of a source's metrics are gauges (levels), else 'activity' (work-per-period)."""
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
    out = []
    for s in sources:
        ms = by_src.get(s["source_id"], [])
        house = "inventory" if ms and all(m["value_type"] == "gauge"
                                          for m in ms) else "activity"
        out.append({"slug": s["slug"], "name": s["name"],
                    "description": s["description"], "house": house,
                    "metrics": ms})
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
