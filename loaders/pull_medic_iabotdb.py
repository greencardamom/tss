#!/usr/bin/env python3
"""pull_medic_iabotdb.py - WaybackMedic's IABot-DB work -> TSS (source 'medic_iabotdb').

Runs on ACRE; reads medic's iabget.done logs from `rabbit` (the VirtualBox host
where the files live on local disk — not via the sheep VM's slow NFS) and turns each
logged IABot-DB update into TSS events. Global (no entity).

DATING — by RUN DATE, per project. Each project's events are bucketed to the mtime
of its `iabget.orig` (when the IABot-DB run was prepared), falling back to
`iabget.done`'s mtime (older projects lack iabget.orig). This is the date the work
actually RAN. (It used to bucket by the date embedded in the project NAME / IMPID,
which is just when the import project was created — often months before the run —
so "work done" landed in the wrong period. That was a bug; this is the fix.)

Per iabget.done line (one update) -> one archive-op event, PLUS a status event
ONLY when the line includes livestateselect (most lines don't):
  archive op : archive_add | archive_modify | archive_delete | archive_unchanged
  status set : set_dead(0) set_alive(3) set_paywall(5) set_permadead(6) set_permalive(7)
The IMPID is still parsed (it classifies the op via the md/a nametype and forms the
ext_key) but is NO LONGER used for the date. Non-modifyurl / unparseable lines are
TRAPPED (logged + non-zero exit); lines without a usable IMPID are skipped (counted).

`find` matches only iabget.done files = COMPLETED projects. A project takes 3-48h;
until it finishes there is no iabget.done, so it's simply picked up on a later run.
We CHECKPOINT per project (its dir basename): after a project's events post, it's
recorded in the done-set, so a crash/re-run re-reads the (fast) stream and re-sends
nothing.

Modes:
  --backfill   rollups DEFERRED; afterwards rebuild_rollups.py medic_iabotdb (py3.11 job).
  (default)    live rollups — for the acre cron.
  --dry-run    read+parse everything and print metric + per-year breakdown; POST
               nothing, write no state. Verify before a hot run.

Idempotent (ext_key = "<IMPID>:<metric>"). Remote read uses `ssh rabbit bash -s`
(rabbit's login shell is tcsh; piping to bash keeps the script tcsh-safe).
Token: --token / --token-file / ~/.config/tss/token_medic_iabotdb / $TSS_MEDIC_IABOTDB_TOKEN.
Stdlib only.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter

import tss_http

HOST = "rabbit"   # VirtualBox HOST; medic's files live on its LOCAL disk.
                  # (sheep is a VM on rabbit; reading via sheep goes over a slow
                  #  VM shared-folder NFS, so read from rabbit directly instead.)
METAIMP = "/home/greenc/sharedNFS/medic/metaimp"   # rabbit-local path
API_DEFAULT = "https://tss.toolforge.org/api/v1"
TOKEN_FILE_DEFAULT = os.path.expanduser("~/.config/tss/token_medic_iabotdb")
STATE_DEFAULT = os.path.expanduser("~/.config/tss/medic_iabotdb.state")

STATUS_MAP = {
    "0": "set_dead", "3": "set_alive", "5": "set_paywall",
    "6": "set_permadead", "7": "set_permalive",   # IABot black/whitelist -> our terms
}

ACTION_RE = re.compile(r"-a\s+(\S+)")
P_RE = re.compile(r"-p\s+'([^']*)'")        # the -p payload (single-quoted)
O_RE = re.compile(r"-o\s+'([^']*)'")        # the -o old-archive-url (OPTIONAL)
IMPID_RE = re.compile(r"IMPID:\s*([^)]+)\)")
# nametype (after the date) may carry a trailing ".cfg"; the oldest few have no
# nametype (those skip). The date group is parsed only to validate the IMPID shape
# -- it is NOT used for bucketing (run-date is).
IMPID_PARTS_RE = re.compile(r"^imp(\d{4,8})(.+)\.(\d{6}-\d{6})\.(\d+)$")


# --- parsing (pure; unit-tested on hand-fed samples) -----------------------

def logical_records(lines):
    """Reassemble physical lines into logical iabget.done records. A few records
    embed literal newlines inside the URL/payload (non-ASCII page titles), so one
    record spans several physical lines. Each record starts with the `iabget.awk`
    invocation (continuation lines never contain it); joining the pieces lets the
    single-quoted -p/-o payloads parse across the embedded newlines instead of the
    continuations trapping as garbage."""
    rec = None
    for ln in lines:
        if "iabget.awk" in ln:
            if rec is not None:
                yield "\n".join(rec)
            rec = [ln]
        elif rec is not None:
            rec.append(ln)
        else:
            yield ln                          # stray leading line (rare) -> as-is
    if rec is not None:
        yield "\n".join(rec)


def parse_line(line):
    """-> (events, status). events: list of {metric, value, ext_key} WITHOUT ts
    (the caller stamps the project's run-date). status: 'ok' | 'skip' | 'trap'."""
    s = line.strip()
    if not s:
        return [], "ok"
    am = ACTION_RE.search(s)
    pm_p = P_RE.search(s)
    if not am or not pm_p:
        return [], "trap"
    if am.group(1) != "modifyurl":
        return [], "trap"                     # other actions: surface (deep past)
    payload = pm_p.group(1)
    om = O_RE.search(s)
    old_o = om.group(1) if om else ""         # -o is OPTIONAL on older lines
    impm = IMPID_RE.search(payload)
    if not impm:
        return [], "skip"                     # no IMPID -> can't classify/key
    impid = impm.group(1).strip()
    pm = IMPID_PARTS_RE.match(impid)
    if not pm:
        return [], "trap"                     # IMPID present but unrecognized shape
    nametype = pm.group(2)
    if nametype.endswith(".cfg"):             # some older projects carry a .cfg suffix
        nametype = nametype[:-4]
    if nametype.endswith("md"):
        proj_type = "md"
    elif nametype.endswith("a"):
        proj_type = "a"
    else:
        return [], "skip"                     # no md/a type -> can't classify op

    params = {}
    for kv in payload.split("{&}"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            params[k] = v

    # Archive op: ALWAYS, by STRUCTURE (reason text is unreliable). Empty
    # archiveurl in an md project sets the DB archive to (none) = delete.
    archiveurl = params.get("archiveurl", "")
    if archiveurl == "":
        op = "archive_delete" if proj_type == "md" else "archive_unchanged"
    elif proj_type == "a":
        op = "archive_add"
    else:  # md, archive present
        op = "archive_unchanged" if archiveurl == old_o else "archive_modify"

    events = [{"metric": op, "value": 1, "ext_key": f"{impid}:{op}"}]

    # Status change is OPTIONAL — most lines only touch the archive.
    ls = params.get("livestateselect", "")
    if ls:
        status = STATUS_MAP.get(ls)
        if status is None:
            return [], "trap"                 # present but unknown code -> surface
        events.append({"metric": status, "value": 1,
                       "ext_key": f"{impid}:{status}"})
    return events, "ok"


# --- token / state ---------------------------------------------------------

def resolve_token(args):
    if args.token:
        return args.token
    path = args.token_file or TOKEN_FILE_DEFAULT
    if os.path.exists(path):
        tok = open(path).read().strip()
        if tok:
            return tok
    return os.environ.get("TSS_MEDIC_IABOTDB_TOKEN")


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


# --- project iteration (yields (project, run_date, lines)) -----------------
#
# run_date = mtime of iabget.orig (when the run was prepared), else iabget.done
# (older projects have no iabget.orig). iabget.done always exists (we find by it),
# so run_date is always resolved.

_SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30", HOST]

# Remote: one project per iabget.done; emit a marker (basename + run-date) then the
# file's lines. Piped to `bash -s` because rabbit's login shell is tcsh.
_REMOTE = r"""
find -L %s -mindepth 2 -maxdepth 2 -name iabget.done -type f | sort | while read -r f; do
  d=$(dirname "$f")
  if [ -e "$d/iabget.orig" ]; then s=$(stat -c %%Y "$d/iabget.orig"); else s=$(stat -c %%Y "$f"); fi
  printf '###P\t%%s\t%%s\n' "$(basename "$d")" "$(date -d @"$s" +%%F)"
  cat "$f"
done
""" % METAIMP


def _run_date_local(dirpath):
    for name in ("iabget.orig", "iabget.done"):
        try:
            mt = os.stat(os.path.join(dirpath, name)).st_mtime
            return datetime.date.fromtimestamp(mt).isoformat()
        except OSError:
            continue
    return None


def iter_projects_local(localdir):
    for root, _dirs, files in sorted(os.walk(localdir)):
        if "iabget.done" in files:
            with open(os.path.join(root, "iabget.done"),
                      encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
            yield os.path.basename(root), _run_date_local(root), lines


def iter_projects_ssh():
    p = subprocess.Popen(_SSH + ["bash -s"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True, errors="replace")
    p.stdin.write(_REMOTE)
    p.stdin.close()
    proj = rd = None
    lines = []
    for line in p.stdout:
        if line.startswith("###P\t"):
            if proj is not None:
                yield proj, rd, lines
            parts = line.rstrip("\n").split("\t")
            proj = parts[1] if len(parts) > 1 else None
            rd = parts[2] if len(parts) > 2 and parts[2] else None
            lines = []
        else:
            lines.append(line)
    if proj is not None:
        yield proj, rd, lines
    p.wait()
    if p.returncode != 0:
        raise RuntimeError(f"ssh project stream failed (rc={p.returncode})")


# --- main ------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="WaybackMedic IABot-DB work -> TSS.")
    ap.add_argument("--backfill", action="store_true",
                    help="rollups DEFERRED (rebuild after); else live rollups")
    ap.add_argument("--api", default=API_DEFAULT)
    ap.add_argument("--token")
    ap.add_argument("--token-file")
    ap.add_argument("--batch-size", type=int, default=10000)
    ap.add_argument("--state", default=STATE_DEFAULT)
    ap.add_argument("--local-dir",
                    help="read from this LOCAL dir tree (rsync iabget.done + "
                         "iabget.orig from rabbit here first) instead of over ssh")
    ap.add_argument("--dry-run", action="store_true",
                    help="read+parse everything and print metric + per-year breakdown, "
                         "but POST nothing and write no state — verify before a hot run")
    args = ap.parse_args()

    token = resolve_token(args)
    if not token and not args.dry_run:
        ap.error("no token (--token, --token-file, ~/.config/tss/token_medic_iabotdb, "
                 "or $TSS_MEDIC_IABOTDB_TOKEN)")

    done = load_done(args.state)
    unknown = []
    tally = Counter()
    tally_year = Counter()

    def on_retry(attempt, wait, reason):
        print(f"    {reason}; retry {attempt} in {wait:.0f}s", file=sys.stderr)

    def post(events):
        url = args.api + "/events" + ("?rollup=defer" if args.backfill else "")
        for i in range(0, len(events), args.batch_size):
            tss_http.post_json(url, token, {"events": events[i:i + args.batch_size]},
                               max_retries=tss_http.BATCH_RETRIES, on_retry=on_retry)

    t0 = time.monotonic()
    nlines = events_seen = skipped_old = 0
    nprojects = ingested = skipped = nodate = total = 0
    PROGRESS_EVERY = 50
    src = f"local {args.local_dir}" if args.local_dir else f"ssh {HOST}"
    print(f"[+0s] reading projects from {src} …", file=sys.stderr, flush=True)
    projects = (iter_projects_local(args.local_dir) if args.local_dir
                else iter_projects_ssh())

    for proj, run_date, lines in projects:
        nprojects += 1
        if proj in done:                       # already posted in a prior run
            skipped += 1
            continue
        if not run_date:                       # no orig/done mtime (shouldn't happen)
            nodate += 1
            continue
        events = []
        for line in logical_records(lines):
            nlines += 1
            evs, status = parse_line(line)
            if status == "trap":
                unknown.append(line.strip())
                continue
            if status == "skip":
                skipped_old += 1
                continue
            for e in evs:
                e["ts"] = run_date             # <-- run-date, not IMPID date
            events.extend(evs)
        if not events:
            continue
        events_seen += len(events)

        if args.dry_run:
            tally.update(e["metric"] for e in events)
            tally_year.update(e["ts"][:4] for e in events)
        else:
            try:
                post(events)
            except (tss_http.FatalHTTP, RuntimeError) as e:
                print(f"\nstopped at {proj}: {e}\n  checkpoint saved "
                      f"({ingested} projects this run); re-run to resume",
                      file=sys.stderr)
                sys.exit(1)
            done.add(proj)
            save_done(args.state, done)        # checkpoint AFTER this project lands
        ingested += 1
        total += len(events)
        if nprojects % PROGRESS_EVERY == 0:
            el = time.monotonic() - t0
            print(f"[+{el:.0f}s] {nprojects} projects, {events_seen:,} events, "
                  f"{ingested} ingested", file=sys.stderr, flush=True)

    mode = "DRY RUN" if args.dry_run else ("backfill" if args.backfill else "poll")
    print(f"{mode}: {nprojects} projects seen, {ingested} ingested ({total} events), "
          f"{skipped} already-done, {nodate} no-date, "
          f"{skipped_old:,} undatable-line skipped, {len(unknown)} trapped")
    if args.dry_run:
        print("events by metric (NOTHING posted):")
        for metric, n in sorted(tally.items()):
            print(f"  {metric:18} {n:>12,}")
        print("events by RUN-DATE year:")
        for yr, n in sorted(tally_year.items()):
            print(f"  {yr}  {n:>12,}")
    elif args.backfill and ingested:
        print("Next: rebuild rollups as a python3.11 job on Toolforge:")
        print("  toolforge jobs run rebuild-medic --image python3.11 --mount all --wait \\")
        print("    --command '$HOME/www/python/venv/bin/python "
              "$HOME/www/python/src/rebuild_rollups.py medic_iabotdb'")

    if unknown:
        print(f"\nTRAP: {len(unknown)} unparsed/non-modifyurl line(s) — sample:",
              file=sys.stderr)
        for s in unknown[:20]:
            print("  " + s[:200], file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
