"""Rollup recomputation.

After events are written, we refresh the affected Day/Month/Year summary rows in
`rollup`. Each summary is recomputed directly from `event` (idempotent and always
correct). For every affected (metric, date) we refresh:

  * the per-entity rows  (entity = 'en', 'commons', ...)  -- only when entity is set
  * the combined total   (entity = '')                    -- sum across all entities

at day / month / year grain.

This recompute-from-source approach is simple and correct; if a batch ever touches
huge numbers of buckets it can be optimised later (incremental deltas / async job).
"""
import datetime


def _month_start(d):
    return d.replace(day=1)


def _month_end(d):
    if d.month == 12:
        return d.replace(day=31)
    return d.replace(month=d.month + 1, day=1) - datetime.timedelta(days=1)


def _year_start(d):
    return d.replace(month=1, day=1)


def _year_end(d):
    return d.replace(month=12, day=31)


def _agg(cur, source_id, metric_id, entity, grain, bucket, start, end):
    """Recompute one rollup row from event rows in [start, end]."""
    if entity == "":
        where = "source_id=%s AND metric_id=%s AND ts BETWEEN %s AND %s"
        params = (source_id, metric_id, start, end)
    else:
        where = "source_id=%s AND metric_id=%s AND entity=%s AND ts BETWEEN %s AND %s"
        params = (source_id, metric_id, entity, start, end)

    cur.execute(
        f"SELECT COALESCE(SUM(value),0) AS v, COUNT(*) AS c FROM event WHERE {where}",
        params,
    )
    row = cur.fetchone()
    cur.execute(
        "INSERT INTO rollup (source_id, metric_id, entity, grain, bucket, value, samples) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE value=VALUES(value), samples=VALUES(samples)",
        (source_id, metric_id, entity, grain, bucket, row["v"], row["c"]),
    )


def recompute(cur, source_id, affected):
    """Refresh all rollups touched by a batch.

    affected: iterable of (metric_id, entity_or_None, datetime.date)
    """
    # Distinct buckets to refresh, split into combined-total ('') and per-entity.
    day_all, mon_all, yr_all = set(), set(), set()
    day_ent, mon_ent, yr_ent = set(), set(), set()

    for metric_id, entity, d in affected:
        day_all.add((metric_id, d))
        mon_all.add((metric_id, _month_start(d)))
        yr_all.add((metric_id, _year_start(d)))
        if entity:
            day_ent.add((metric_id, entity, d))
            mon_ent.add((metric_id, entity, _month_start(d)))
            yr_ent.add((metric_id, entity, _year_start(d)))

    for m, d in day_all:
        _agg(cur, source_id, m, "", "day", d, d, d)
    for m, d in mon_all:
        _agg(cur, source_id, m, "", "month", d, d, _month_end(d))
    for m, d in yr_all:
        _agg(cur, source_id, m, "", "year", d, d, _year_end(d))

    for m, e, d in day_ent:
        _agg(cur, source_id, m, e, "day", d, d, d)
    for m, e, d in mon_ent:
        _agg(cur, source_id, m, e, "month", d, d, _month_end(d))
    for m, e, d in yr_ent:
        _agg(cur, source_id, m, e, "year", d, d, _year_end(d))


# Period-start expressions for set-based rebuilds. Literal % is doubled (%%)
# because PyMySQL treats % as the parameter marker.
_GRAINS = (
    ("day", "ts"),
    ("month", "DATE_FORMAT(ts, '%%Y-%%m-01')"),
    ("year", "DATE_FORMAT(ts, '%%Y-01-01')"),
)


def _inlist(ids):
    # ids are trusted integer metric_ids from our own DB -> safe to inline
    # (and keeps the IN(...) out of the param list, which PyMySQL can't expand).
    return ",".join(str(int(i)) for i in ids)


def _rebuild_sum(cur, source_id, grain, bucket, mfilter):
    """Flow metrics: a bucket's value = SUM of its events (the original path).
    `mfilter` is '' (all metrics) or ' AND metric_id IN (...)'. When a source is
    entirely 'sum' metrics, mfilter='' and these are the exact pre-gauge queries."""
    cur.execute(
        f"INSERT INTO rollup (source_id, metric_id, entity, grain, bucket, value, samples) "
        f"SELECT source_id, metric_id, entity, %s, {bucket}, SUM(value), COUNT(*) "
        f"FROM event WHERE source_id = %s AND entity IS NOT NULL AND entity <> ''{mfilter} "
        f"GROUP BY source_id, metric_id, entity, {bucket}",
        (grain, source_id),
    )
    cur.execute(
        f"INSERT INTO rollup (source_id, metric_id, entity, grain, bucket, value, samples) "
        f"SELECT source_id, metric_id, '', %s, {bucket}, SUM(value), COUNT(*) "
        f"FROM event WHERE source_id = %s{mfilter} "
        f"GROUP BY source_id, metric_id, {bucket}",
        (grain, source_id),
    )


def _rebuild_last(cur, source_id, grain, bucket, metric_ids):
    """Gauge/level metrics: a bucket's value = the LAST reading in the period.

    Two axes, two rules: across TIME take the latest snapshot (ROW_NUMBER by
    ts desc); across ENTITIES the combined ('') total SUMS each entity's last
    reading (a 'total across all wikis' row). Summing a level over time would be
    nonsense, hence the window pick. Gauge sources must be entity-keyed.
    """
    ids = _inlist(metric_ids)
    win = f"PARTITION BY metric_id, entity, {bucket}"
    # per-entity: each entity's latest reading within the bucket
    cur.execute(
        f"INSERT INTO rollup (source_id, metric_id, entity, grain, bucket, value, samples) "
        f"SELECT source_id, metric_id, entity, %s, bucket, value, samples FROM ("
        f"  SELECT source_id, metric_id, entity, {bucket} AS bucket, value, "
        f"         COUNT(*) OVER ({win}) AS samples, "
        f"         ROW_NUMBER() OVER ({win} ORDER BY ts DESC, event_id DESC) AS rn "
        f"  FROM event "
        f"  WHERE source_id = %s AND entity IS NOT NULL AND entity <> '' AND metric_id IN ({ids}) "
        f") t WHERE rn = 1",
        (grain, source_id),
    )
    # combined total: sum across entities of each entity's last reading
    cur.execute(
        f"INSERT INTO rollup (source_id, metric_id, entity, grain, bucket, value, samples) "
        f"SELECT source_id, metric_id, '', %s, bucket, SUM(value), SUM(samples) FROM ("
        f"  SELECT source_id, metric_id, entity, {bucket} AS bucket, value, "
        f"         COUNT(*) OVER ({win}) AS samples, "
        f"         ROW_NUMBER() OVER ({win} ORDER BY ts DESC, event_id DESC) AS rn "
        f"  FROM event "
        f"  WHERE source_id = %s AND entity IS NOT NULL AND entity <> '' AND metric_id IN ({ids}) "
        f") t WHERE rn = 1 "
        f"GROUP BY source_id, metric_id, bucket",
        (grain, source_id),
    )


def rebuild_source(cur, source_id):
    """Recompute ALL rollups for one source from scratch, set-based.

    Far faster than per-bucket recompute for bulk loads/backfills: a handful of
    GROUP BY scans instead of millions of small queries. Deletes the source's
    rollups first so removed events don't leave stale rows. Can run for minutes
    on a large source, so prefer running it on Toolforge (job/bastion) rather
    than over HTTP.

    Each metric is consolidated per its `default_agg`:
      * 'sum'  - flows (work done per period); additive over time. The original
                 fast path; for an all-sum source the SQL is byte-identical to
                 the pre-gauge engine.
      * 'last' - gauges/levels (an absolute reading); latest snapshot per period,
                 combined total = sum of per-entity lasts. (See _rebuild_last.)
    avg/max/min/counter aren't implemented yet -> raise rather than write wrong
    numbers. NOTE: the live per-write recompute() path is sum-only, so gauge
    sources MUST load with ?rollup=defer and then rebuild (this function).
    """
    cur.execute("DELETE FROM rollup WHERE source_id = %s", (source_id,))
    cur.execute("SELECT metric_id, default_agg FROM metric WHERE source_id = %s",
                (source_id,))
    aggs = {r["metric_id"]: r["default_agg"] for r in cur.fetchall()}
    sum_ids = [m for m, a in aggs.items() if a == "sum"]
    last_ids = [m for m, a in aggs.items() if a == "last"]
    unsupported = sorted(set(aggs.values()) - {"sum", "last"})
    if unsupported:
        raise NotImplementedError(
            f"rollup default_agg not implemented yet: {unsupported} "
            f"(source_id {source_id})")
    only_sum = bool(sum_ids) and not last_ids   # every existing flow source
    for grain, bucket in _GRAINS:
        if sum_ids:
            _rebuild_sum(cur, source_id, grain, bucket,
                         "" if only_sum else f" AND metric_id IN ({_inlist(sum_ids)})")
        if last_ids:
            _rebuild_last(cur, source_id, grain, bucket, last_ids)
