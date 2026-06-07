#!/usr/bin/env python3
"""monitor_tss.py - data-freshness check for TSS sources.

Exits non-zero (with a message) if a source has not received new data within its
allowed age, so a scheduled job run with `--emails onfailure` mails the
maintainer. Age is computed in SQL (NOW() vs MAX(created_at), both DB time) to
avoid any Python/DB timezone skew.

Per-source thresholds (hours):
  iabotwatch : live uploader every ~15 min -> expect data within a few hours
  booksup    : daily pull -> expect data within ~2 days

Run as a python3.11 job, e.g. hourly:
  toolforge jobs run monitor-tss --image python3.11 --mount all \
    --schedule '@hourly' --emails onfailure \
    --command '$HOME/www/python/venv/bin/python $HOME/www/python/src/monitor_tss.py'
"""
import argparse
import sys

import pymysql
import pymysql.cursors

import config

# slug -> default max age in hours
DEFAULT_LIMITS = {"iabotwatch": 6.0, "booksup": 48.0}


def main():
    ap = argparse.ArgumentParser(description="TSS source freshness check.")
    ap.add_argument("--iabw-hours", type=float, default=DEFAULT_LIMITS["iabotwatch"])
    ap.add_argument("--booksup-hours", type=float, default=DEFAULT_LIMITS["booksup"])
    args = ap.parse_args()
    limits = {"iabotwatch": args.iabw_hours, "booksup": args.booksup_hours}

    conn = pymysql.connect(
        host=config.DB_HOST, port=config.DB_PORT,
        read_default_file=config.REPLICA_CNF, database=config.DB_NAME,
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.slug, MAX(e.created_at) AS last_at, "
                "       TIMESTAMPDIFF(MINUTE, MAX(e.created_at), NOW()) / 60.0 AS age_h "
                "FROM source s LEFT JOIN event e ON e.source_id = s.source_id "
                "GROUP BY s.slug"
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    alerts = []
    for r in rows:
        slug = r["slug"]
        limit = limits.get(slug)
        if limit is None:
            continue  # source not monitored
        if r["last_at"] is None:
            print(f"{slug}: NO DATA at all (limit {limit}h)")
            alerts.append(f"{slug}: no data at all")
            continue
        age = float(r["age_h"])
        state = "STALE" if age > limit else "ok"
        print(f"{slug}: last event {age:.1f}h ago (limit {limit}h) [{state}]")
        if age > limit:
            alerts.append(f"{slug}: {age:.1f}h since last event (limit {limit}h)")

    if alerts:
        print("ALERT: " + "; ".join(alerts), file=sys.stderr)
        sys.exit(1)
    print("all monitored sources fresh")


if __name__ == "__main__":
    main()
