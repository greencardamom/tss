#!/usr/bin/env python3
"""pull_medic_enwiki.py - WaybackMedic's enwiki dead-link repairs -> TSS (source 'medic_enwiki').

Runs on SLATER (cron 03:50, --since-days 60). WaybackMedic fixes dead links on
English Wikipedia in per-project directories under
`rabbit:/home/greenc/sharedNFS/medic/meta/<name>.<range>/`. The per-project tcsh
`stats` script reports the headline numbers. Rather than run `stats` (it needs
sheep-only softredir rulesets + a per-project arg, and the oldest projects predate
`stats` entirely), we compute the SAME numbers straight from each project's logs.

Where the compute happens: ONE `ssh rabbit bash -s` stream runs a tiny script over
rabbit's LOCAL disk (fast) and returns one compact summary line per project. The
big logs (syslog/urlchanger can be tens of thousands of lines x thousands of
projects) NEVER cross the network and nothing is copied to /beater. (sheep reaches
these same files only via a slow shared-folder NFS — so we read rabbit directly,
the lesson from the iabotapi/iabotdb work.)

Metric  <- file / computation (matches the `stats` script's variables):
  pages_edited        <- wc -l discovered.orig                          (disci)
  archives_added      <- wc -l newialink + wc -l newaltarch             (nai)
  archives_proactive  <- newialink rows split on " ---- " with 3+ fields whose
                         last field holds the tag "proactive" (archive added to a
                         live link)
  archives_reactive   <- archives_added - archives_proactive (untagged/old rows +
                         all newaltarch: archive repairs a dead link)
  status_to_live      <- grep 'url-status live' urlchanger, MINUS rows ending
                         urlchanger1.54/1.56 (written by a cs12 make-live, an
                         intermediary step - undoing a medic error or preparing a
                         cite for later work - not counted as work done anywhere)
  status_to_dead      <- grep -c 'url-status dead' urlchanger           (urlsdi)
  links_moved         <- sum of 6 syslog 'urlchanger7.1.NN{A,B,I,H,D,F}' counts
                         (convi); normal-redirects is [1-9]B not [0-9]B ("Don't
                         count 7.1.0B not redirects"). PLUS, only for projects
                         with no 7.1.N<CAPITAL> row, old-form "Redirect found"
                         moves (syslog, else urlchanger; never both)
  soft404_resolved    <- grep -Ec 'urlchanger7.1.[0-9]{1,2}(T|S)' syslog (soft404i)
  refs_converted      <- grep -Ec 'llmconv1[.][12]$' logllmconv          (llmconvi)
  usurped             <- grep -Ec 'fixiats20[.][1246]' logiats            (usurpi)
  deadlink_removed    <- grep -c 'removed dead link' urlchanger          (rdl)
  deadlink_added      <- grep -Ec 'urlchangerL[.](4|6)[A-Za-z]?$' urlchanger (adli;
                         no domain grep: urlchangerL rows are domain-limited at log time)
  metadata_changed    <- grep -Ec 'urlchanger5[.](4|6)$' urlchanger      (metai)
  templates_converted <- wc -l unwindtl                                  (uwtli)
  access_status_added <- grep -Ec 'urlchangerSUB[.](4|6)$' urlchanger (url-access=
                         subscription/limited or {{subscription}}/{{limited access}}
                         added; id only - not SUB.5/5b, not logiats iatsWebarchiveAccess)

START DATES (the START table below): before a metric's start date nothing is sent
for it, even when its log exists - the feature was not logged yet (or, for
urlchanger/syslog ids before 2024, the id meant something else). Before
archives_proactive's start, proactive archives were not logged at all.

FILE-PRESENCE = DATA-PRESENCE. A metric is emitted ONLY when its source log
exists. An absent log means the feature did not exist yet for that (older)
project -> NO DATA (omit), NOT a zero. A present log with a genuine 0 count IS
emitted as 0. So pages_edited/archives_added/archives_reactive go back ~2015;
refs_converted and templates_converted start when their own logs exist; the rest
start at the later of file presence and their START date.

A project is "finished" exactly when discovered.orig exists: the push script writes
it only after edits land on enwiki. That same file's mtime is the event date
(falling back to Documentation's mtime). Global (no entity).

Modes:
  --backfill   rollups DEFERRED; afterwards rebuild_rollups.py medic_enwiki (py3.11 job).
  (default)    live rollups - for the slater cron.
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
           "status_to_dead", "links_moved", "archives_reactive",
           "archives_proactive", "soft404_resolved", "refs_converted",
           "deadlink_removed", "usurped", "metadata_changed",
           "templates_converted", "deadlink_added", "access_status_added")

# Remote compute. For each project dir holding a discovered.orig (= finished),
# emit ONE tab line:  project<TAB>date<TAB><one field per METRICS entry, same order>
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

  # archives: added = every row (history stays continuous); split by tag.
  # newialink row: "article----wurl ---- location ---- tags" -> split on " ---- "
  # gives 3 fields when tags exist. proactive = 3+ fields and the last holds the
  # word "proactive". Everything else (untagged/old formats) and all of
  # newaltarch is reactive.
  arch="-"; areact="-"; apro="-"
  if [ -e "$dir/newialink" ] || [ -e "$dir/newaltarch" ]; then
    nia=0; pro=0; [ -e "$dir/newialink" ]  && nia=$(wc -l < "$dir/newialink")
    [ -e "$dir/newialink" ] && pro=$(awk -F' ---- ' 'NF>=3 && $NF ~ /(^| )proactive( |$)/{n++} END{print n+0}' "$dir/newialink")
    naa=0; [ -e "$dir/newaltarch" ] && naa=$(wc -l < "$dir/newaltarch")
    arch=$((nia + naa)); apro=$pro; areact=$((arch - pro))
  fi

  # status_to_live excludes the url-status-live rows that a cs12 make-live
  # writes (urlchanger1.54/1.56) - make-live is an intermediary step, not work done.
  sl="-"; sd="-"; rdl="-"; meta="-"; adl="-"; sub="-"
  if [ -e "$dir/urlchanger" ]; then
    sl=$(grep 'url-status live' "$dir/urlchanger" | grep -Evc 'urlchanger1[.]5[46]$')
    sd=$(grep -c 'url-status dead' "$dir/urlchanger")
    rdl=$(grep -c 'removed dead link' "$dir/urlchanger")
    meta=$(grep -Ec 'urlchanger5[.](4|6)$' "$dir/urlchanger")
    adl=$(grep -Ec 'urlchangerL[.](4|6)[A-Za-z]?$' "$dir/urlchanger")
    # id only: the text "url-access" also appears in urlchanger0.0 dumps and 5.7 enrich rows
    sub=$(grep -Ec 'urlchangerSUB[.](4|6)$' "$dir/urlchanger")
  fi

  moved="-"; s404="-"
  if [ -e "$dir/syslog" ]; then
    r=$(grep -Ec 'urlchanger7.1.[0-9]{1,2}A' "$dir/syslog")
    n=$(grep -Ec 'urlchanger7.1.[1-9]{1,2}B' "$dir/syslog")
    i=$(grep -Ec 'urlchanger7.1.[0-9]{1,2}I' "$dir/syslog")
    g=$(grep -Ec 'urlchanger7.1.[0-9]{1,2}H' "$dir/syslog")
    c=$(grep -Ec 'urlchanger7.1.[0-9]{1,2}D' "$dir/syslog")
    f=$(grep -Ec 'urlchanger7.1.[0-9]{1,2}F' "$dir/syslog")
    moved=$((r + n + i + g + c + f))
    s404=$(grep -Ec 'urlchanger7.1.[0-9]{1,2}(T|S)' "$dir/syslog")
  fi

  # Old-form moves (before the 7.1 letter codes): "Redirect found: check it out" /
  # "WM Redirect found..." / "Redirect found del .asp" with id urlchanger7.1[.N[a-z]].
  # Only for a project with NO urlchanger7.1.N<CAPITAL> row in syslog. Count in
  # syslog; if that gives 0, count in urlchanger instead - never add the two, both
  # logs hold the same move. Other 7.1 messages (Redirect to home/soft-404) are not moves.
  OLDRE=' ---- (WM )?Redirect found[^-]* ---- urlchanger7[.]1([.][0-9]+[a-z]?)?$'
  if [ -e "$dir/syslog" ] || [ -e "$dir/urlchanger" ]; then
    newform=0; old=0
    [ -e "$dir/syslog" ] && newform=$(grep -Ec 'urlchanger7[.]1[.][0-9]+[A-Z]' "$dir/syslog")
    if [ "$newform" -eq 0 ]; then
      [ -e "$dir/syslog" ] && old=$(grep -Ec "$OLDRE" "$dir/syslog")
      [ "$old" -eq 0 ] && [ -e "$dir/urlchanger" ] && old=$(grep -Ec "$OLDRE" "$dir/urlchanger")
    fi
    [ "$moved" = "-" ] && moved=0
    moved=$((moved + old))
  fi

  llm="-"; [ -e "$dir/logllmconv" ] && llm=$(grep -Ec 'llmconv1[.][12]$' "$dir/logllmconv")
  usurp="-"; [ -e "$dir/logiats" ] && usurp=$(grep -Ec 'fixiats20[.][1246]' "$dir/logiats")
  uwtl="-"; [ -e "$dir/unwindtl" ] && uwtl=$(wc -l < "$dir/unwindtl")

  # field order == METRICS order
  printf '%%s\t%%s' "$proj" "$date"
  printf '\t%%s' "$pages" "$arch" "$sl" "$sd" "$moved" "$areact" "$apro" \
    "$s404" "$llm" "$rdl" "$usurp" "$meta" "$uwtl" "$adl" "$sub"
  printf '\n'
done
''' % META


# START: per-metric date before which the metric is NOT sent (no data -> a gap on
# the chart); from it on a 0 is a real 0. Set from the first project whose logs hold
# the feature's own rows (checked 2026-11-25 against all 2,421 finished projects,
# sampling the matched rows). Metrics not listed rely on file presence alone.
START = {
    # first "add url-status live" row (urlchanger1.2b): thecanadianencyclopedia, 2023-06-09
    "status_to_live":   "2023-06-09",
    # first "url-status dead" row: adelaide2, 2024-01-13
    "status_to_dead":   "2024-01-13",
    # first "removed dead link" row: betalatimes, 2024-04-12
    "deadlink_removed": "2024-04-12",
    # first L.4/L.6 row; every sampled match since reads "add {{dead link}}": msnbc, 2019-11-16
    "deadlink_added":   "2019-11-16",
    # first 5.4 row; pre-2024 matches are "remove stray domain in work.etc field" (metadata): danish, 2020-05-03
    "metadata_changed": "2020-05-03",
    # first finished project (by date) with an old-form move row ("Redirect found ... urlchanger7.1"): msnbc.0051-0150, 2019-11-08
    "links_moved":      "2019-11-08",
    # first 7.1 T/S soft-404 row in syslog: espngocom, 2024-07-17
    "soft404_resolved": "2024-07-17",
    # first finished projects with proactive rows (thehinducom2 passA/passC); the controller
    # backfills every finished batch with newialinkbf, so untagged after this = a real 0
    "archives_proactive": "2026-09-25",
    # first project with urlchangerSUB.4/.6 ids (url-access / {{subscription}} added)
    "access_status_added": "2026-09-25",
    # first fixiats20 row in logiats (logiats itself exists from 2017): geoftp, 2020-06-16
    "usurped":          "2020-06-16",
}

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
        if len(parts) != 2 + len(METRICS):
            continue
        proj, date = parts[0], parts[1]
        if not proj or not date:
            continue
        vals = {}
        for metric, field in zip(METRICS, parts[2:]):
            if date < START.get(metric, ""):
                continue                      # before the metric's start: no data
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
    yr_have = defaultdict(Counter)        # year -> metric -> #projects reporting it

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
                yr_have[yr][m] += 1
        buf.extend(events)
        buf_projects.append(proj)
        if len(buf) >= args.batch_size:
            flush()
    flush()

    mode = "DRY RUN" if args.dry_run else ("backfill" if args.backfill else "poll")
    print(f"{mode}: {seen} finished projects seen, {ingested} ingested "
          f"({total_events} events), {skipped} already-done")
    if args.dry_run:
        # one row per metric, one column per year (15 metrics don't fit across);
        # "-" = no project sent the metric that year (no data), 0 = a real zero
        yrs = sorted(by_year)
        print("\n  %-20s" % "metric" + "".join(f"{y:>10}" for y in yrs))
        print("  %-20s" % "projects" + "".join(f"{len(yr_projects[y]):>10,}" for y in yrs))
        for m in METRICS:
            print("  %-20s" % m + "".join(
                f"{by_year[y][m]:>10,}" if yr_have[y][m] else f"{'-':>10}" for y in yrs))
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
