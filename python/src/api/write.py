"""Write endpoints: event ingestion (per-source token) + optional registration."""
import datetime
import json

from flask import Blueprint, request, jsonify, g

import config
import rollup
from auth import require_source_token, require_admin_token, hash_token
from db import get_db

bp = Blueprint("write", __name__)


def _metric_map(cur, source_id):
    cur.execute("SELECT metric_id, slug FROM metric WHERE source_id=%s", (source_id,))
    return {r["slug"]: r["metric_id"] for r in cur.fetchall()}


@bp.post("/events")
@require_source_token
def post_events():
    src = g.source
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("events"), list):
        return jsonify(error="body must be {\"events\": [...]}"), 400

    events = body["events"]
    if not events:
        return jsonify(written=0, source=src["slug"])
    if len(events) > config.MAX_BATCH:
        return jsonify(error=f"batch too large (max {config.MAX_BATCH})"), 400

    db = get_db()
    cur = db.cursor()
    mmap = _metric_map(cur, src["source_id"])

    rows, affected = [], set()
    for i, e in enumerate(events):
        if not isinstance(e, dict):
            return jsonify(error=f"event {i} is not an object"), 422
        try:
            metric_slug = e["metric"]
            ts = e["ts"]
            value = e["value"]
            ext_key = e["ext_key"]
        except KeyError as missing:
            return jsonify(error=f"event {i} missing field {missing}"), 422

        mid = mmap.get(metric_slug)
        if not mid:
            return jsonify(error=f"event {i}: unknown metric '{metric_slug}'"), 422
        try:
            d = datetime.date.fromisoformat(ts)
        except (ValueError, TypeError):
            return jsonify(error=f"event {i}: ts must be YYYY-MM-DD"), 422

        entity = e.get("entity")
        ref_id = e.get("ref_id")
        dims = e.get("dims")
        dims_json = json.dumps(dims) if dims is not None else None

        rows.append(
            (src["source_id"], mid, entity, ts, value, ref_id, dims_json, ext_key)
        )
        affected.add((mid, entity, d))

    # Bulk loads pass ?rollup=defer to skip per-batch recompute; the caller then
    # rebuilds rollups once at the end (rebuild_source / rebuild-rollups endpoint).
    defer = request.args.get("rollup") == "defer"

    try:
        cur.executemany(
            "INSERT INTO event "
            "(source_id, metric_id, entity, ts, value, ref_id, dims, ext_key) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE metric_id=VALUES(metric_id), entity=VALUES(entity), "
            "value=VALUES(value), ref_id=VALUES(ref_id), dims=VALUES(dims)",
            rows,
        )
        if not defer:
            rollup.recompute(cur, src["source_id"], affected)
        db.commit()
    except Exception:
        db.rollback()
        raise

    return jsonify(written=len(rows), source=src["slug"], rollup="deferred" if defer else "updated")


@bp.post("/sources/<slug>/rebuild-rollups")
@require_source_token
def rebuild_rollups(slug):
    """Full rollup rebuild for the authenticated source.

    Convenient for small sources; for a large initial backfill prefer running
    python/src/rebuild_rollups.py on Toolforge so it can't hit the HTTP timeout.
    """
    if g.source["slug"] != slug:
        return jsonify(error="token does not match source"), 403
    db = get_db()
    cur = db.cursor()
    rollup.rebuild_source(cur, g.source["source_id"])
    db.commit()
    return jsonify(source=slug, status="rollups rebuilt")


# --- optional registration (registration can also be done via sql/seed.sql) ---

@bp.post("/sources")
@require_admin_token
def create_source():
    b = request.get_json(silent=True) or {}
    if not b.get("slug") or not b.get("name"):
        return jsonify(error="slug and name are required"), 400
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO source (slug, name, description, ref_url_tpl) "
        "VALUES (%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE name=VALUES(name), description=VALUES(description), "
        "ref_url_tpl=VALUES(ref_url_tpl)",
        (b["slug"], b["name"], b.get("description"), b.get("ref_url_tpl")),
    )
    # If a write token was supplied, store its hash.
    if b.get("api_token"):
        cur.execute(
            "UPDATE source SET api_token_hash=%s WHERE slug=%s",
            (hash_token(b["api_token"]), b["slug"]),
        )
    db.commit()
    return jsonify(slug=b["slug"]), 201


@bp.post("/sources/<slug>/metrics")
@require_admin_token
def create_metrics(slug):
    metrics = request.get_json(silent=True)
    if not isinstance(metrics, list) or not metrics:
        return jsonify(error="body must be a non-empty list of metrics"), 400
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT source_id FROM source WHERE slug=%s", (slug,))
    src = cur.fetchone()
    if not src:
        return jsonify(error=f"unknown source '{slug}'"), 404
    for m in metrics:
        cur.execute(
            "INSERT INTO metric "
            "(source_id, slug, label, unit, value_type, default_agg, category) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE label=VALUES(label), unit=VALUES(unit), "
            "value_type=VALUES(value_type), default_agg=VALUES(default_agg), "
            "category=VALUES(category)",
            (
                src["source_id"],
                m["slug"],
                m.get("label", m["slug"]),
                m.get("unit", ""),
                m.get("value_type", "count"),
                m.get("default_agg", "sum"),
                m.get("category"),
            ),
        )
    db.commit()
    return jsonify(source=slug, metrics=len(metrics)), 201
