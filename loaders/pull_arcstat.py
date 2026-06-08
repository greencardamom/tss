#!/usr/bin/env python3
"""pull_arcstat.py - Archive-URL inventory across wikis -> TSS (source 'arcstat').

The FIRST gauge/level source. Each line of quepasa:~/toolforge/arcstat/db/master.db
is a per-site snapshot ("as of date D, site X holds N wayback links, ..."), NOT
work-done-per-period. Format (4 space-separated fields):

    <site> <YYYYMMDD> <content_pages> <v1|v2|...|v16>

The leading count is `content_pages`; the 16 pipe positions map, in order, to:
  1 wayback_links     2 pages_wayback      3 altarchive_links  4 pages_altarchive
  5 archiveis_links   6 pages_archiveis    7 webcite_links     8 pages_webcite
  9 googlebooks_links 10 media_texts      11 media_audio      12 media_movies
 13 media_image      14 media_other       15 media_texts_paged 16 media_dark

PRESENCE = DATA. Older rows have only 15 positions (field 16 `media_dark` was added
later) -> media_dark is NO DATA for those, not 0; we simply don't emit it. A handful
of truncated lines (<15 positions) are skipped + reported, never half-loaded.

GAUGE handling: events are posted with ?rollup=defer, then a rebuild is triggered
(rebuild_source consolidates gauges by 'last': latest snapshot per period, combined
total = sum of per-site lasts). The live per-write recompute path is sum-only, so we
must NEVER post arcstat without defer. master.db is tiny (~4k lines), so each run
re-posts everything (idempotent via ext_key = "<site>:<date>:<metric>") and rebuilds
the whole source — no checkpoint needed.

Modes:
  (default)    post all events (deferred) + trigger a rebuild via the API.
  --no-rebuild post only; rebuild yourself (e.g. the Toolforge rebuild job) — used
               for the first load so the new gauge rollup can be verified with --wait.
  --dry-run    parse + print a per-metric summary incl. "current inventory" (sum of
               each site's latest reading) to eyeball against the dashboard totals;
               POST nothing.

Token: --token / --token-file / ~/.tss_token_arcstat / $TSS_ARCSTAT_TOKEN. Stdlib only.
"""
import argparse
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict

import tss_http

HOST = "quepasa"
REMOTE_FILE = "/home/greenc/toolforge/arcstat/db/master.db"
API_DEFAULT = "https://tss.toolforge.org/api/v1"
TOKEN_FILE_DEFAULT = os.path.expanduser("~/.tss_token_arcstat")

# pipe position (0-based) -> metric slug. content_pages is the standalone leading
# count, handled separately. Order is append-only in master.db, so position is stable.
PIPE_METRICS = [
    "wayback_links", "pages_wayback", "altarchive_links", "pages_altarchive",
    "archiveis_links", "pages_archiveis", "webcite_links", "pages_webcite",
    "googlebooks_links", "media_texts", "media_audio", "media_movies",
    "media_image", "media_other", "media_texts_paged", "media_dark",
]
MIN_PIPE = 15   # 15 or 16 = normal; fewer = truncated/corrupt -> skip + report


def resolve_token(args):
    if args.token:
        return args.token
    path = args.token_file or TOKEN_FILE_DEFAULT
    if os.path.exists(path):
        tok = open(path).read().strip()
        if tok:
            return tok
    return os.environ.get("TSS_ARCSTAT_TOKEN")


def read_lines(args):
    if args.local_file:
        with open(args.local_file, encoding="utf-8", errors="replace") as fh:
            yield from fh
        return
    p = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30", HOST,
         "cat " + REMOTE_FILE],
        stdout=subprocess.PIPE, text=True, errors="replace")
    yield from p.stdout
    p.wait()
    if p.returncode != 0:
        raise RuntimeError(f"ssh cat failed (rc={p.returncode})")


def parse_line(line):
    """-> (site, ts, [(slug, value), ...], status). status: 'ok' | 'blank' | 'short'."""
    s = line.strip()
    if not s:
        return None, None, [], "blank"
    parts = s.split()
    if len(parts) != 4:
        return None, None, [], "short"
    site, date, npages, pipe = parts
    if len(date) != 8 or not date.isdigit():
        return None, None, [], "short"
    ts = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    vals = pipe.split("|")
    if len(vals) < MIN_PIPE:
        return None, None, [], "short"

    metrics = []
    if npages.lstrip("-").isdigit():
        metrics.append(("content_pages", int(npages)))
    for i, v in enumerate(vals):
        if i >= len(PIPE_METRICS):
            break                         # ignore any positions beyond 16
        v = v.strip()
        if v.lstrip("-").isdigit():       # present + numeric -> real reading
            metrics.append((PIPE_METRICS[i], int(v)))
    return site, ts, metrics, "ok"


def main():
    ap = argparse.ArgumentParser(description="Archive-URL counts (gauge) -> TSS.")
    ap.add_argument("--api", default=API_DEFAULT)
    ap.add_argument("--token")
    ap.add_argument("--token-file")
    ap.add_argument("--batch-size", type=int, default=10000)
    ap.add_argument("--local-file", help="read master.db from a local path (testing)")
    ap.add_argument("--no-rebuild", action="store_true",
                    help="post (deferred) but DON'T trigger a rebuild — do it yourself")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse + print a summary; POST nothing")
    args = ap.parse_args()

    token = resolve_token(args)
    if not token and not args.dry_run:
        ap.error("no token (--token, --token-file, ~/.tss_token_arcstat, "
                 "or $TSS_ARCSTAT_TOKEN)")

    events = []
    nlines = nshort = nblank = 0
    dates = []
    sites = set()
    per_metric = Counter()                       # metric -> #readings
    latest = defaultdict(dict)                    # site -> {metric: (date, value)}

    for line in read_lines(args):
        nlines += 1
        site, ts, metrics, status = parse_line(line)
        if status == "blank":
            nblank += 1
            continue
        if status == "short":
            nshort += 1
            continue
        sites.add(site)
        dates.append(ts)
        for slug, val in metrics:
            events.append({"metric": slug, "ts": ts, "value": val,
                           "entity": site, "ext_key": f"{site}:{ts}:{slug}"})
            per_metric[slug] += 1
            prev = latest[site].get(slug)
            if prev is None or ts > prev[0]:     # track each site's most-recent reading
                latest[site][slug] = (ts, val)

    span = f"{min(dates)} .. {max(dates)}" if dates else "(none)"
    print(f"parsed {nlines} lines: {len(sites)} sites, {len(events)} events, "
          f"{span}; {nshort} short/skipped, {nblank} blank")

    if args.dry_run:
        # "current inventory" = sum over sites of each site's latest reading
        all_metrics = ["content_pages"] + PIPE_METRICS
        print("\n  metric              readings   current_total (sum of each site's latest)")
        for m in all_metrics:
            cur_total = sum(latest[site][m][1] for site in latest if m in latest[site])
            print(f"  {m:18} {per_metric[m]:>8}   {cur_total:>16,}")
        if nshort:
            print(f"\n  NOTE: {nshort} truncated/odd lines skipped (<{MIN_PIPE} pipe fields).")
        return

    def on_retry(attempt, wait, reason):
        print(f"    {reason}; retry {attempt} in {wait:.0f}s", file=sys.stderr)

    # Always defer: gauge rollups are only correct via rebuild_source.
    url = args.api + "/events?rollup=defer"
    for i in range(0, len(events), args.batch_size):
        tss_http.post_json(url, token, {"events": events[i:i + args.batch_size]},
                           max_retries=tss_http.BATCH_RETRIES, on_retry=on_retry)
    print(f"posted {len(events)} events (deferred)")

    if args.no_rebuild:
        print("Next: rebuild rollups (gauge-aware) as a python3.11 job on Toolforge:")
        print("  toolforge jobs run rebuild-arcstat --image python3.11 --mount all --wait \\")
        print("    --command '$HOME/www/python/venv/bin/python "
              "$HOME/www/python/src/rebuild_rollups.py arcstat'")
        return

    print("triggering rebuild (gauge-aware) via API ...")
    tss_http.post_json(args.api + "/sources/arcstat/rebuild-rollups", token, {},
                       max_retries=tss_http.BATCH_RETRIES, on_retry=on_retry)
    print("rebuild done")


if __name__ == "__main__":
    main()
