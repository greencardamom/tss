#!/usr/bin/env python3
"""pull_medic_enwiki.py - WaybackMedic's enwiki dead-link repairs -> TSS (source 'medic_enwiki').

Runs on ACRE. WaybackMedic fixes dead links on English Wikipedia in per-project
directories under `rabbit:/home/greenc/sharedNFS/medic/meta/<name>.<range>/`. The
per-project tcsh `stats` script reports several headline numbers; TSS tracks the
five that matter. Rather than run `stats` (it needs sheep-only softredir rulesets
+ a per-project arg, and the oldest projects predate `stats` entirely), we compute
the SAME numbers straight from each project's logs.

Where the compute happens: ONE `ssh rabbit bash -s` stream runs a tiny script over
rabbit's LOCAL disk (fast) and returns one compact summary line per project. The
big logs (syslog/urlchanger can be tens of thousands of lines x thousands of
projects) NEVER cross the network and nothing is copied to /beater. (sheep reaches
these same files only via a slow shared-folder NFS — so we read rabbit directly,
the lesson from the iabotapi/iabotdb work.)

Metric  <- file / computation (matches the `stats` script's variables):
  pages_edited   <- wc -l discovered.orig                         (stats: disci)
  archives_added <- wc -l newialink + wc -l newaltarch            (stats: nai)
  status_to_live <- grep -c 'url-status live' urlchanger          (stats: urlsli)
  status_to_dead <- grep -c 'url-status dead' urlchanger          (stats: urlsdi)
  links_moved    <- sum of 5 syslog 'urlchanger7.1.NN{A,B,I,H,D}' (stats: convi)
                    counts; note normal-redirects is [1-9] not [0-9] (the script
                    comments "Don't count 7.1.0B not redirects").

FILE-PRESENCE = DATA-PRESENCE. A metric is emitted ONLY when its source log
exists. An absent log means the feature did not exist yet for that (older)
project -> NO DATA (omit), NOT a zero. A present log with a genuine 0 count IS
emitted as 0. So pages_edited/archives_added go back ~2015, status_* appear once
`urlchanger` exists (~2021+), links_moved once `syslog` has 7.1 redirects (softredir
era, ~Nov-2024+).

A project is "finished" exactly when discovered.orig exists: the push script writes
it only after edits land on enwiki. That same file's mtime is the event date
(falling back to Documentation's mtime). Global (no entity).

Modes:
  --backfill   rollups DEFERRED; afterwards rebuild_rollups.py medic_enwiki (py3.11 job).
  (default)    live rollups - for the acre cron.
  --dry-run    compute everything and print a per-year breakdown; POST nothing,
               write no state. Verify before a hot run.
  --since-days N  only look at projects whose discovered.orig changed in the last N
               days (cron: skip re-grepping all history each night). Backfill omits it.

Idempotent (ext_key = "<project>:<metric>"); checkpointed per project in
~/.config/tss/medic_enwiki.state so a crash/re-run re-reads the (fast) stream and re-sends
nothing. Remote cmds kept tcsh-safe (rabbit's login shell is tcsh; we pipe the
script to `bash -s` so tcsh never parses it). Stdlib only.
Token: --token / --token-file / ~/.config/tss/token_medic_enwiki / $TSS_MEDIC_ENWIKI_TOKEN.
"""
import argparse
import os
import json
import subprocess
import sys
import time
from collections import Counter, defaultdict

import tss_http

HOST = "rabbit"   # VirtualBox HOST; medic's files live on its LOCAL disk.
META = "/home/greenc/sharedNFS/medic/meta"   # rabbit-local path
API_DEFAULT = "https://tss.toolforge.org/api/v1"
TOKEN_FILE_DEFAULT = os.path.expanduser("~/.config/tss/token_medic_enwiki")
STATE_DEFAULT = os.path.expanduser("~/.config/tss/medic_enwiki.state")

METRICS = ("pages_edited", "archives_added", "status_to_live",
           "status_to_dead", "links_moved")

# Remote compute. For each project dir holding a discovered.orig (= finished),
# emit ONE tab line:  project<TAB>date<TAB>pages<TAB>archives<TAB>live<TAB>dead<TAB>moved
# A field is the integer count, or "-" when the source log is ABSENT (no data).
# $1 (optional) is a find -newermt spec, e.g. "@1700000000", to limit to recent
# projects; empty = every project. grep -c prints 0 (exit 1) on no match - fine,
# we don't set -e, so the 0 is captured.
REMOTE = r'''
META="%s"
SINCE="${1:-}"
if [ -n "$SINCE" ]; then
  FIND=(find "$META" -mindepth 2 -maxdepth 2 -name discovered.orig -newermt "$SINCE" -printf '%%h\n')
else
  FIND=(find "$META" -mindepth 2 -maxdepth 2 -name discovered.orig -printf '%%h\n')
fi
"${FIND[@]}" | sort | while IFS= read -r dir; do
  proj=$(basename "$dir")
  pages=$(wc -l < "$dir/discovered.orig" 2>/dev/null)
  [ -z "$pages" ] && continue
  if [ -e "$dir/discovered.orig" ]; then dt="$dir/discovered.orig"; else dt="$dir/Documentation"; fi
  date=$(date -d @"$(stat -c %%Y "$dt")" +%%F 2>/dev/null)

  arch="-"
  if [ -e "$dir/newialink" ] || [ -e "$dir/newaltarch" ]; then
    nia=0; [ -e "$dir/newialink" ]  && nia=$(wc -l < "$dir/newialink")
    naa=0; [ -e "$dir/newaltarch" ] && naa=$(wc -l < "$dir/newaltarch")
    arch=$((nia + naa))
  fi

  sl="-"; sd="-"
  if [ -e "$dir/urlchanger" ]; then
    sl=$(grep -c 'url-status live' "$dir/urlchanger")
    sd=$(grep -c 'url-status dead' "$dir/urlchanger")
  fi

  moved="-"
  if [ -e "$dir/syslog" ]; then
    r=$(grep -Ec 'urlchanger7.1.[0-9]{1,2}A' "$dir/syslog")
    n=$(grep -Ec 'urlchanger7.1.[1-9]{1,2}B' "$dir/syslog")
    i=$(grep -Ec 'urlchanger7.1.[0-9]{1,2}I' "$dir/syslog")
    g=$(grep -Ec 'urlchanger7.1.[0-9]{1,2}H' "$dir/syslog")
    c=$(grep -Ec 'urlchanger7.1.[0-9]{1,2}D' "$dir/syslog")
    moved=$((r + n + i + g + c))
  fi

  printf '%%s\t%%s\t%%s\t%%s\t%%s\t%%s\t%%s\n' "$proj" "$date" "$pages" "$arch" "$sl" "$sd" "$moved"
done
''' % META


_SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30", HOST]


def resolve_token(args):
    if args.token:
        return args.token
    path = args.token_file or TOKEN_FILE_DEFAULT
    if os.path.exists(path):
        tok = open(path).read().strip()
        if tok:
            return tok
    return os.environ.get("TSS_MEDIC_ENWIKI_TOKEN")


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


def stream(since_arg):
    """Yield parsed project rows from the remote compute over ssh.

    Each row -> (project, date, {metric: int} for present metrics only).
    A "-" field means the source log was absent -> that metric is omitted.
    """
    remote = "bash -s -- " + since_arg if since_arg else "bash -s"
    p = subprocess.Popen(_SSH + [remote], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True, errors="replace")
    p.stdin.write(REMOTE)
    p.stdin.close()
    for line in p.stdout:
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 7:
            continue
        proj, date, pages, arch, sl, sd, moved = parts
        if not proj or not date:
            continue
        vals = {}
        for metric, field in zip(METRICS, (pages, arch, sl, sd, moved)):
            if field != "-" and field != "":
                try:
                    vals[metric] = int(field)
                except ValueError:
                    pass
        yield proj, date, vals
    p.wait()
    if p.returncode != 0:
        raise RuntimeError(f"ssh remote compute failed (rc={p.returncode})")


def main():
    ap = argparse.ArgumentParser(description="WaybackMedic enwiki repairs -> TSS.")
    ap.add_argument("--backfill", action="store_true",
                    help="rollups DEFERRED (rebuild after); else live rollups")
    ap.add_argument("--api", default=API_DEFAULT)
    ap.add_argument("--token")
    ap.add_argument("--token-file")
    ap.add_argument("--batch-size", type=int, default=10000)
    ap.add_argument("--state", default=STATE_DEFAULT)
    ap.add_argument("--since-days", type=int,
                    help="only projects whose discovered.orig changed in the last N "
                         "days (cron, to skip re-grepping all history); omit for all")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute + print a per-year breakdown, POST nothing, write "
                         "no state - verify before a hot run")
    args = ap.parse_args()

    token = resolve_token(args)
    if not token and not args.dry_run:
        ap.error("no token (--token, --token-file, ~/.config/tss/token_medic_enwiki, "
                 "or $TSS_MEDIC_ENWIKI_TOKEN)")

    since_arg = ""
    if args.since_days:
        since_arg = "@%d" % int(time.time() - args.since_days * 86400)

    done = load_done(args.state)

    def on_retry(attempt, wait, reason):
        print(f"    {reason}; retry {attempt} in {wait:.0f}s", file=sys.stderr)

    def post(events):
        url = args.api + "/events" + ("?rollup=defer" if args.backfill else "")
        for i in range(0, len(events), args.batch_size):
            tss_http.post_json(url, token, {"events": events[i:i + args.batch_size]},
                               max_retries=tss_http.BATCH_RETRIES, on_retry=on_retry)

    # dry-run accounting
    by_year = defaultdict(Counter)        # year -> metric -> summed value
    yr_projects = defaultdict(set)        # year -> set(project)
    have = Counter()                      # metric -> #projects reporting it

    buf = []            # pending events
    buf_projects = []   # projects contributing to buf (for checkpoint)
    ingested = skipped = total_events = 0

    def flush():
        nonlocal buf, buf_projects, ingested, total_events
        if not buf:
            buf_projects = []
            return
        if not args.dry_run:
            try:
                post(buf)
            except (tss_http.FatalHTTP, RuntimeError) as e:
                print(f"\nstopped: {e}\n  checkpoint saved ({ingested} projects done "
                      f"this run); re-run to resume", file=sys.stderr)
                sys.exit(1)
            done.update(buf_projects)
            save_done(args.state, done)   # checkpoint AFTER these projects land
        ingested += len(buf_projects)
        total_events += len(buf)
        buf = []
        buf_projects = []

    t0 = time.monotonic()
    seen = 0
    src = f"ssh {HOST}:{META}" + (f" (last {args.since_days}d)" if args.since_days else "")
    print(f"[+0s] computing project stats on {src} …", file=sys.stderr, flush=True)
    for proj, date, vals in stream(since_arg):
        seen += 1
        if not vals:
            continue
        if proj in done:                  # already posted in a prior run
            skipped += 1
            continue
        events = [{"metric": m, "ts": date, "value": v, "ext_key": f"{proj}:{m}"}
                  for m, v in vals.items()]
        if args.dry_run:
            yr = date[:4]
            yr_projects[yr].add(proj)
            for m, v in vals.items():
                by_year[yr][m] += v
                have[m] += 1
        buf.extend(events)
        buf_projects.append(proj)
        if len(buf) >= args.batch_size:
            flush()
    flush()

    mode = "DRY RUN" if args.dry_run else ("backfill" if args.backfill else "poll")
    print(f"{mode}: {seen} finished projects seen, {ingested} ingested "
          f"({total_events} events), {skipped} already-done")
    if args.dry_run:
        print("\n  year   projects   pages_edited  archives_added  to_live  to_dead  links_moved")
        for yr in sorted(by_year):
            c = by_year[yr]
            print(f"  {yr}   {len(yr_projects[yr]):>7}   "
                  f"{c['pages_edited']:>12,}  {c['archives_added']:>14,}  "
                  f"{c['status_to_live']:>7,}  {c['status_to_dead']:>7,}  "
                  f"{c['links_moved']:>11,}")
        print("\n  projects reporting each metric (file present):")
        for m in METRICS:
            print(f"    {m:16} {have[m]:>6}")
    elif args.backfill and ingested:
        print("Next: rebuild rollups as a python3.11 job on Toolforge:")
        print("  toolforge jobs run rebuild-medic-enwiki --image python3.11 --mount all --wait \\")
        print("    --command '$HOME/www/python/venv/bin/python "
              "$HOME/www/python/src/rebuild_rollups.py medic_enwiki'")


if __name__ == "__main__":
    main()
