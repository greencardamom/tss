#!/usr/bin/env python3
"""monitor_tss.py - data-freshness check for TSS sources.

Exits non-zero (with a message) if a source has not received new data within its
allowed age, so a scheduled job run with `--emails onfailure` mails the
maintainer. Age is computed in SQL (NOW() vs MAX(created_at), both DB time) to
avoid any Python/DB timezone skew.

Per-source thresholds (hours):
  eventstreams : live uploader every ~15 min -> expect data within a few hours
  booksup      : daily pull -> expect data within ~2 days
  iabotapi     : daily pull -> expect data within ~2 days

Exit codes (1 and 2 both mail via `--emails onfailure`, but mean different things):
  0  all monitored sources fresh
  1  STALE  - a source is past its age limit (the real alert)
  2  MONITOR ERROR - ToolsDB unreachable/erroring, freshness NOT checked (infra,
     not data). Kept distinct so an infra blip is never mistaken for stale data.

Run as a python3.11 job, e.g. hourly:
  toolforge jobs run monitor-tss --image python3.11 --mount all \
    --schedule '@hourly' --emails onfailure --timeout 300 --retry 1 \
    --command '$HOME/www/python/venv/bin/python $HOME/www/python/src/monitor_tss.py'
"""
import argparse
import sys
import time

import pymysql
import pymysql.cursors

import config

# slug -> default max age in hours
DEFAULT_LIMITS = {"eventstreams": 6.0, "booksup": 48.0, "iabotapi": 48.0}

# DB access is bounded and retried. Without read_timeout pymysql blocks forever:
# a wedged ToolsDB once left this hourly job running 2h before the server reset
# the connection (uncaught OperationalError 2013 -> traceback -> exit 1).
DB_CONNECT_TIMEOUT = 10     # seconds to establish the connection
DB_READ_TIMEOUT    = 60     # seconds to wait for query results (indexed query is ms)
DB_WRITE_TIMEOUT   = 10
DB_ATTEMPTS        = 3      # transient resets are common on shared ToolsDB
DB_BACKOFF         = 5      # seconds, multiplied by the attempt number
# Worst case in-process: 3*60 read + (5+10) backoff = ~195s, which must stay
# comfortably under the job's --timeout 300 so k8s never kills us mid-retry.

EXIT_STALE    = 1
EXIT_DBERROR  = 2

# MAX(created_at) per source. Written as a correlated subquery (not a LEFT JOIN +
# GROUP BY over the whole event table) so it uses ix_event_freshness
# (source_id, created_at) -- one index dive per source instead of a full scan of
# 37M+ rows. NOW() stays in SQL so age is DB-time throughout (no TZ skew).
QUERY = (
    "SELECT slug, last_at, TIMESTAMPDIFF(MINUTE, last_at, NOW()) / 60.0 AS age_h "
    "FROM ( "
    "  SELECT s.slug AS slug, "
    "         (SELECT MAX(e.created_at) FROM event e WHERE e.source_id = s.source_id) AS last_at "
    "  FROM source s "
    ") t"
)


class DBUnavailable(Exception):
    """ToolsDB could not be reached/queried after DB_ATTEMPTS tries."""


def fetch_rows():
    """Run QUERY with bounded timeouts, retrying transient connection errors."""
    last_err = None
    for attempt in range(1, DB_ATTEMPTS + 1):
        conn = None
        try:
            conn = pymysql.connect(
                host=config.DB_HOST, port=config.DB_PORT,
                read_default_file=config.REPLICA_CNF, database=config.DB_NAME,
                charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
                connect_timeout=DB_CONNECT_TIMEOUT,
                read_timeout=DB_READ_TIMEOUT,
                write_timeout=DB_WRITE_TIMEOUT,
            )
            with conn.cursor() as cur:
                cur.execute(QUERY)
                return cur.fetchall()
        except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as e:
            last_err = e
            print(f"db attempt {attempt}/{DB_ATTEMPTS} failed: {e}", file=sys.stderr)
            if attempt < DB_ATTEMPTS:
                time.sleep(DB_BACKOFF * attempt)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    raise DBUnavailable(last_err)


def main():
    ap = argparse.ArgumentParser(description="TSS source freshness check.")
    ap.add_argument("--eventstreams-hours", type=float, default=DEFAULT_LIMITS["eventstreams"])
    ap.add_argument("--booksup-hours", type=float, default=DEFAULT_LIMITS["booksup"])
    ap.add_argument("--iabotapi-hours", type=float, default=DEFAULT_LIMITS["iabotapi"])
    args = ap.parse_args()
    limits = {
        "eventstreams": args.eventstreams_hours,
        "booksup": args.booksup_hours,
        "iabotapi": args.iabotapi_hours,
    }

    try:
        rows = fetch_rows()
    except DBUnavailable as e:
        # Infra failure, NOT a staleness alert: we never got to check the data.
        print(f"MONITOR ERROR: ToolsDB unreachable after {DB_ATTEMPTS} attempts "
              f"-- freshness NOT checked: {e}", file=sys.stderr)
        sys.exit(EXIT_DBERROR)

    alerts = []
    for r in rows:
        slug = r["slug"]
        limit = limits.get(slug)
        if limit is None:
            continue  # source not monitored
        if r["last_at"] is None:
            # Registered but never reported yet (e.g. adapter not live). Not a
            # staleness failure — stay quiet until the first data arrives.
            print(f"{slug}: no data yet [ok] (not alerting until first data)")
            continue
        age = float(r["age_h"])
        state = "STALE" if age > limit else "ok"
        print(f"{slug}: last event {age:.1f}h ago (limit {limit}h) [{state}]")
        if age > limit:
            alerts.append(f"{slug}: {age:.1f}h since last event (limit {limit}h)")

    if alerts:
        print("ALERT: " + "; ".join(alerts), file=sys.stderr)
        sys.exit(EXIT_STALE)
    print("all monitored sources fresh")


if __name__ == "__main__":
    main()
