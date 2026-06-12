#!/usr/bin/env python3
"""pull_numberofurl.py - external-URL inventory across all wikis -> TSS ('numberofurl').

A GAUGE source, sibling of arcstat: per-site LEVELS ("as of date D, site X holds N
wayback URLs"), value_type=gauge / default_agg=last. The data is the Commons tabular
page Data:Wikipedia_statistics/exturls.tab (a JSON object: schema.fields + data rows),
regenerated ~every 30 days (on the 15th) by the numberofurl bot on acre, which also
leaves a local copy at /home/greenc/toolforge/numberofurl/datau.tab.

Each data row is one site: [site, <16 numeric fields>]. Metric slugs are the .tab
schema field names verbatim (pages, urls, uniqurls, waybackurls, ...). The total.*
rows (total.all, total.wikipedia, ...) are aggregates -> SKIPPED (TSS computes the
combined _all itself). No per-row date: the whole snapshot shares one date.

History lives in the Commons page's REVISIONS. The first RELIABLE snapshot is
2025-10-18; everything before it (Oct-2025 setup churn) is filtered out by MIN_DATE.
After that it's monthly on the 15th.

Modes:
  --backfill   pull the Commons revision history (>= MIN_DATE); each snapshot dated by
               its revision timestamp. Posts deferred; --no-rebuild to rebuild by hand.
  (default)    read the local datau.tab (acre), dated by its embedded "Last update";
               skip if that snapshot date is already loaded (state file) -> a daily
               cron is a cheap no-op until the monthly file changes. Posts deferred +
               triggers the gauge rebuild via the API.
  --dry-run    parse + summary (incl. sum-over-sites vs the file's own total.all row as
               a parse check); POST nothing.

GAUGE: only rebuild_source consolidates 'last' correctly; the live recompute path is
sum-only -> always post ?rollup=defer then rebuild. Idempotent (ext_key =
"<site>:<date>:<slug>"). Token: --token / --token-file / ~/.config/tss/token_numberofurl /
$TSS_NUMBEROFURL_TOKEN. Stdlib only.
"""
import argparse
import datetime
import json
import os
import re
import sys
from collections import Counter

import tss_http
import tss_wiki

LOCAL_FILE = "/home/greenc/toolforge/numberofurl/datau.tab"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
PAGE = "Data:Wikipedia_statistics/exturls.tab"
MIN_DATE = "2025-10-18"      # first reliable snapshot; earlier revisions are setup churn
API_DEFAULT = "https://tss.toolforge.org/api/v1"
TOKEN_FILE_DEFAULT = os.path.expanduser("~/.config/tss/token_numberofurl")
STATE_DEFAULT = os.path.expanduser("~/.config/tss/numberofurl.state")

_DESC_DATE = re.compile(r"Last update:\s*(.+?)\s*$")


def resolve_token(args):
    if args.token:
        return args.token
    path = args.token_file or TOKEN_FILE_DEFAULT
    if os.path.exists(path):
        tok = open(path).read().strip()
        if tok:
            return tok
    return os.environ.get("TSS_NUMBEROFURL_TOKEN")


def load_done(path):
    if path and os.path.exists(path):
        return set(json.load(open(path)).get("done", []))
    return set()


def save_done(path, done):
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"done": sorted(done)}, fh)
    os.replace(tmp, path)


def embedded_date(obj):
    """Snapshot date from description 'Last update: Fri May 15 08:20:01 UTC 2026'."""
    desc = (obj.get("description") or {}).get("en", "")
    m = _DESC_DATE.search(desc)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(
            m.group(1).strip(), "%a %b %d %H:%M:%S UTC %Y").date().isoformat()
    except ValueError:
        return None


def parse_tab(obj):
    """-> (rows, totals).  rows: [(site, [(slug, int), ...]), ...] (real wikis only);
    totals: {slug: int} from the file's own total.all row (for a dry-run parse check)."""
    fields = [f["name"] for f in obj["schema"]["fields"]]
    site_i = fields.index("site")
    metric_cols = [(i, n) for i, n in enumerate(fields) if n != "site"]
    rows, totals = [], {}
    for row in obj.get("data", []):
        site = row[site_i]
        ms = [(n, int(row[i])) for i, n in metric_cols
              if isinstance(row[i], (int, float))]
        if site == "total.all":
            totals = dict(ms)
            continue
        if site.startswith("total."):
            continue                      # per-family aggregates -> TSS recomputes
        rows.append((site, ms))
    return rows, totals


def fetch_revisions():
    """Yield (date_iso, obj) for each Commons revision with content."""
    for rev in tss_wiki.fetch_page_revisions(
            PAGE, COMMONS_API, rvprop="timestamp|content", rvslots="main"):
        date = rev["timestamp"][:10]
        content = rev.get("slots", {}).get("main", {}).get("*")
        if not content:
            continue
        try:
            obj = json.loads(content)
        except ValueError:
            continue
        yield date, obj


def events_from(rows, date):
    return [{"metric": slug, "ts": date, "value": v, "entity": site,
             "ext_key": f"{site}:{date}:{slug}"}
            for site, ms in rows for slug, v in ms]


def main():
    ap = argparse.ArgumentParser(description="numberofurl external-URL counts (gauge) -> TSS.")
    ap.add_argument("--backfill", action="store_true",
                    help="load Commons revision history (>= %s)" % MIN_DATE)
    ap.add_argument("--api", default=API_DEFAULT)
    ap.add_argument("--token")
    ap.add_argument("--token-file")
    ap.add_argument("--file", default=LOCAL_FILE, help="local datau.tab (cron mode)")
    ap.add_argument("--batch-size", type=int, default=10000)
    ap.add_argument("--state", default=STATE_DEFAULT)
    ap.add_argument("--no-rebuild", action="store_true",
                    help="post (deferred) but DON'T trigger a rebuild")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse + summary; POST nothing")
    args = ap.parse_args()

    token = resolve_token(args)
    if not token and not args.dry_run:
        ap.error("no token (--token, --token-file, ~/.config/tss/token_numberofurl, "
                 "or $TSS_NUMBEROFURL_TOKEN)")
    done = load_done(args.state)

    # --- gather snapshots: (date, rows, totals) ---
    snapshots = []
    if args.backfill:
        for date, obj in fetch_revisions():
            if date < MIN_DATE:
                continue
            rows, totals = parse_tab(obj)
            snapshots.append((date, rows, totals))
        snapshots.sort()                         # oldest first
    else:
        obj = json.load(open(args.file, encoding="utf-8"))
        date = embedded_date(obj)
        if not date:
            print("could not read snapshot date from datau.tab description", file=sys.stderr)
            sys.exit(1)
        rows, totals = parse_tab(obj)
        snapshots.append((date, rows, totals))

    if not snapshots:
        print("no snapshots found")
        return

    # --- dry-run: summary + parse check vs the file's own total.all ---
    if args.dry_run:
        for date, rows, totals in snapshots:
            evs = events_from(rows, date)
            print(f"{date}: {len(rows)} sites, {len(evs)} events")
        date, rows, totals = snapshots[-1]
        sums = Counter()
        for _site, ms in rows:
            for slug, v in ms:
                sums[slug] += v
        print(f"\n  latest snapshot {date}: sum-over-sites vs file's total.all "
              f"(should match)")
        print(f"  {'metric':22} {'computed':>16} {'total.all':>16}  ok")
        for slug in sorted(sums):
            tv = totals.get(slug)
            ok = "==" if tv == sums[slug] else "!!"
            print(f"  {slug:22} {sums[slug]:>16,} {('' if tv is None else format(tv,',')):>16}  {ok}")
        return

    def on_retry(attempt, wait, reason):
        print(f"    {reason}; retry {attempt} in {wait:.0f}s", file=sys.stderr)

    # --- post (deferred) ---
    posted_dates = []
    new = 0
    for date, rows, totals in snapshots:
        if not args.backfill and date in done:
            print(f"snapshot {date} already loaded — nothing to do")
            return
        evs = events_from(rows, date)
        url = args.api + "/events?rollup=defer"
        for i in range(0, len(evs), args.batch_size):
            tss_http.post_json(url, token, {"events": evs[i:i + args.batch_size]},
                               max_retries=tss_http.BATCH_RETRIES, on_retry=on_retry)
        print(f"posted {date}: {len(evs)} events")
        posted_dates.append(date)
        new += len(evs)

    if not posted_dates:
        print("nothing new to post")
        return
    print(f"posted {new} events across {len(posted_dates)} snapshot(s) (deferred)")

    # --- rebuild (gauge-aware) ---
    if args.no_rebuild:
        print("Next: rebuild rollups (gauge-aware) as a python3.11 job on Toolforge:")
        print("  toolforge jobs run rebuild-numberofurl --image python3.11 --mount all --wait \\")
        print("    --command '$HOME/www/python/venv/bin/python "
              "$HOME/www/python/src/rebuild_rollups.py numberofurl'")
    else:
        print("triggering rebuild (gauge-aware) via API ...")
        tss_http.post_json(args.api + "/sources/numberofurl/rebuild-rollups", token, {},
                           max_retries=tss_http.BATCH_RETRIES, on_retry=on_retry)
        print("rebuild done")

    done.update(posted_dates)
    save_done(args.state, done)


if __name__ == "__main__":
    main()
