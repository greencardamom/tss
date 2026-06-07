#!/usr/bin/env python3
"""pull_iabotapi.py - pull authoritative IABot stats into TSS (source 'iabotapi').

Fetches IABot's own statistics API (action=statistics, anonymous), one request
per (year, month) covering all wikis, maps each per-wiki-per-day row to TSS
events, and POSTs them.

The IABot API allows ~5 requests/minute and runs on a busy, resource-constrained
server, so requests are SERIAL and paced (default 12s apart = 5/min), with the
shared hardened retry/Retry-After backoff (tss_http) on top.

Modes:
  --backfill        full history (2015-01 .. current month), rollups DEFERRED,
                    resumable. Afterwards rebuild rollups as a job:
                      toolforge jobs run rebuild-iabotapi --image python3.11 \
                        --mount all --wait --command '$HOME/www/python/venv/bin/python \
                        $HOME/www/python/src/rebuild_rollups.py iabotapi'
  (default)         recent months only (--months N, default 2), rollups LIVE.
                    Intended for the daily Toolforge job.

Override the range with --from / --to (YYYY-MM). Idempotent
(ext_key = "<wiki>:<date>:<metric>"); re-running overwrites in place.

Token: --token, --token-file, ~/.tss_token_iabotapi, or $TSS_IABOTAPI_TOKEN.
Stdlib only.
"""
import argparse
import datetime
import os
import sys
import time

import tss_http

IABOT_API = "https://iabot.wmcloud.org/api.php"
API_DEFAULT = "https://tss.toolforge.org/api/v1"
TOKEN_FILE_DEFAULT = os.path.expanduser("~/.tss_token_iabotapi")
STATE_DEFAULT = os.path.expanduser("~/.tss_iabotapi.state")

# IABot API field -> TSS metric slug. The Total* fields are derived sums and are
# NOT stored (computed on read), matching the seed.
FIELD_MAP = {
    "DeadLinks": "dead_links",
    "LiveLinks": "live_links",
    "TagLinks": "tag_links",
    "UnknownLinks": "unknown_links",
    "DeadEdits": "dead_edits",
    "ProactiveEdits": "proactive_edits",
    "ReactiveEdits": "reactive_edits",
    "UnknownEdits": "unknown_edits",
}


# --- token / state ---------------------------------------------------------

def resolve_token(args):
    """iabotapi token only -- never fall back to another source's token."""
    if args.token:
        return args.token
    path = args.token_file or TOKEN_FILE_DEFAULT
    if os.path.exists(path):
        tok = open(path).read().strip()
        if tok:
            return tok
    return os.environ.get("TSS_IABOTAPI_TOKEN")


def load_done(path):
    if path and os.path.exists(path):
        import json
        return set(json.load(open(path)).get("done", []))
    return set()


def save_done(path, done):
    if not path:
        return
    import json
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"done": sorted(done)}, fh)
    os.replace(tmp, path)


# --- fetch + map -----------------------------------------------------------

def _on_retry(attempt, wait, reason):
    print(f"    {reason}; retry {attempt} in {wait:.0f}s", file=sys.stderr)


def fetch_month(year, month):
    """Return the statistics list for one (year, month), all wikis.

    Empty periods (e.g. before IABot had activity) come back as
    {"result":"fail","statistics":[]} -- that's "no data", not an error, so we
    return [] and let the caller skip. Only a structurally unexpected response
    (not a dict / no statistics array) is treated as an error.
    """
    url = (f"{IABOT_API}?action=statistics"
           f"&only-year={year}&only-month={month}&format=flat")
    data = tss_http.get_json(url, max_retries=tss_http.BATCH_RETRIES,
                             on_retry=_on_retry)
    if not isinstance(data, dict) or "statistics" not in data:
        raise RuntimeError(
            f"IABot API unexpected response for {year}-{month:02d}: {str(data)[:200]}")
    stats = data.get("statistics")
    return stats if isinstance(stats, list) else []


def rows_to_events(rows):
    events = []
    for row in rows:
        wiki = row.get("Wiki")
        ts = row.get("Timestamp")
        if not wiki or not ts:
            continue
        date = ts.split(" ", 1)[0]  # "2024-06-01 00:00:00" -> "2024-06-01"
        for api_field, slug in FIELD_MAP.items():
            try:
                value = int(row.get(api_field, 0))
            except (TypeError, ValueError):
                value = 0
            events.append({
                "metric": slug,
                "entity": wiki,
                "ts": date,
                "value": value,
                "ext_key": f"{wiki}:{date}:{slug}",
            })
    return events


# --- range helpers ---------------------------------------------------------

def parse_ym(s):
    y, m = s.split("-")
    return (int(y), int(m))


def months_between(start, end):
    """Yield (year, month) from start (y,m) to end (y,m) inclusive."""
    y, m = start
    while (y, m) <= end:
        yield y, m
        m += 1
        if m > 12:
            m, y = 1, y + 1


def main():
    today = datetime.date.today()
    ap = argparse.ArgumentParser(description="Pull IABot API stats into TSS.")
    ap.add_argument("--backfill", action="store_true",
                    help="full history 2015-01..now, rollups deferred, resumable")
    ap.add_argument("--months", type=int, default=2,
                    help="(non-backfill) how many recent months to refresh, live")
    ap.add_argument("--from", dest="frm", help="override start YYYY-MM")
    ap.add_argument("--to", dest="to", help="override end YYYY-MM")
    ap.add_argument("--api", default=API_DEFAULT)
    ap.add_argument("--token")
    ap.add_argument("--token-file")
    ap.add_argument("--min-interval", type=float, default=12.0,
                    help="seconds between IABot requests (12 = 5/min)")
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument("--state", default=STATE_DEFAULT,
                    help="resume file (backfill only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the request plan; no API calls")
    args = ap.parse_args()

    end = parse_ym(args.to) if args.to else (today.year, today.month)
    if args.frm:
        start = parse_ym(args.frm)
    elif args.backfill:
        start = (2015, 1)
    else:
        # recent N months back from current
        y, m = today.year, today.month
        for _ in range(max(args.months, 1) - 1):
            m -= 1
            if m < 1:
                m, y = 12, y - 1
        start = (y, m)

    defer = args.backfill
    chunks = list(months_between(start, end))
    print(f"iabotapi pull: {start[0]}-{start[1]:02d}..{end[0]}-{end[1]:02d} "
          f"({len(chunks)} month-requests), "
          f"{'DEFERRED (backfill)' if defer else 'LIVE'}, "
          f"pacing {args.min_interval:.0f}s")

    if args.dry_run:
        return

    token = resolve_token(args)
    if not token:
        ap.error("no iabotapi token (--token, --token-file, "
                 "~/.tss_token_iabotapi, or $TSS_IABOTAPI_TOKEN)")

    state_path = args.state if defer else None  # only resume during backfill
    done = load_done(state_path)
    post_url = f"{args.api}/events" + ("?rollup=defer" if defer else "")

    total_events = sent_chunks = 0
    last_fetch = 0.0
    for (year, month) in chunks:
        key = f"{year}-{month:02d}"
        if key in done:
            continue
        # pace: at least min-interval between IABot requests
        gap = args.min_interval - (time.monotonic() - last_fetch)
        if gap > 0:
            time.sleep(gap)
        last_fetch = time.monotonic()

        try:
            rows = fetch_month(year, month)
        except tss_http.FatalHTTP as e:
            print(f"\nFATAL fetching {key}: {e}", file=sys.stderr)
            sys.exit(1)
        events = rows_to_events(rows)
        if not events:
            print(f"  {key}: no data")
            done.add(key)
            save_done(state_path, done)
            continue

        try:
            for i in range(0, len(events), args.batch_size):
                tss_http.post_json(post_url, token,
                                   {"events": events[i:i + args.batch_size]},
                                   max_retries=tss_http.BATCH_RETRIES,
                                   on_retry=_on_retry)
        except tss_http.FatalHTTP as e:
            print(f"\nFATAL posting {key}: {e}\n  Check the iabotapi token and "
                  "that seed.sql loaded the iabotapi metrics.", file=sys.stderr)
            sys.exit(1)

        done.add(key)
        save_done(state_path, done)
        total_events += len(events)
        sent_chunks += 1
        print(f"  {key}: {len(rows)} rows -> {len(events)} events sent")

    print(f"\ndone: {sent_chunks} month(s), {total_events} events")
    if defer and total_events:
        print("Next: rebuild iabotapi rollups as a python3.11 job:")
        print("  toolforge jobs run rebuild-iabotapi --image python3.11 --mount all --wait \\")
        print("    --command '$HOME/www/python/venv/bin/python "
              "$HOME/www/python/src/rebuild_rollups.py iabotapi'")


if __name__ == "__main__":
    main()
