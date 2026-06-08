#!/usr/bin/env python3
"""pull_medic_iabotdb.py - WaybackMedic's IABot-DB work -> TSS (source 'medic_iabotdb').

Runs on ACRE; reads medic's iabget.done logs from `rabbit` (the VirtualBox host
where the files live on local disk — not via the sheep VM's slow NFS) in ONE ssh stream
(`find … -exec cat +`) and turns each logged IABot-DB update into TSS events.
Global (no entity); daily buckets by the project-name date in each line's IMPID.

Per iabget.done line (one update) -> one archive-op event, PLUS a status event
ONLY when the line includes livestateselect (most lines don't):
  archive op : archive_add | archive_modify | archive_delete | archive_unchanged
  status set : set_dead(0) set_alive(3) set_paywall(5) set_permadead(6) set_permalive(7)
Non-modifyurl / unparseable lines are TRAPPED (logged + non-zero exit) so odd
historic formats surface instead of being silently dropped.

`find` matches only iabget.done files = COMPLETED projects. A project takes 3-48h;
until it finishes there is no iabget.done, so it simply isn't streamed yet and gets
picked up on a later run (no pending/abandon bookkeeping needed). We CHECKPOINT per
project: the stream is grouped by IMPID project dir, and after a project's events
post, the dir is recorded in the done-set (save_state). A crash/re-run only
re-reads the (fast) stream and skips already-done projects — nothing re-sent.

Modes:
  --backfill   rollups DEFERRED; afterwards rebuild_rollups.py medic_iabotdb (py3.11 job).
  (default)    live rollups — for the acre cron.
  --dry-run    read+parse everything and print the metric breakdown; POST nothing,
               write no state. Verify before a hot run.

Idempotent (ext_key = "<IMPID>:<metric>"). Remote cmds kept tcsh/POSIX-safe.
Token: --token / --token-file / ~/.tss_token_medic_iabotdb / $TSS_MEDIC_IABOTDB_TOKEN.
Stdlib only.
"""
import argparse
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
TOKEN_FILE_DEFAULT = os.path.expanduser("~/.tss_token_medic_iabotdb")
STATE_DEFAULT = os.path.expanduser("~/.tss_medic_iabotdb.state")

STATUS_MAP = {
    "0": "set_dead", "3": "set_alive", "5": "set_paywall",
    "6": "set_permadead", "7": "set_permalive",   # IABot black/whitelist -> our terms
}

ACTION_RE = re.compile(r"-a\s+(\S+)")
PO_RE = re.compile(r"-p\s+'(.*)'\s+-o\s+'(.*)'\s*$")     # payload, old-archive-url
IMPID_RE = re.compile(r"IMPID:\s*([^)]+)\)")
IMPID_PARTS_RE = re.compile(r"^imp(\d{8})(.+)\.(\d{6}-\d{6})\.(\d+)$")


# --- parsing (pure; unit-tested on hand-fed samples) -----------------------

def parse_line(line):
    """-> (events, project_dir, ok). ok=False => trap; events=[] ok=True => blank."""
    s = line.strip()
    if not s:
        return [], None, True
    am = ACTION_RE.search(s)
    pom = PO_RE.search(s)
    if not am or not pom:
        return [], None, False
    action = am.group(1)
    payload, old_o = pom.group(1), pom.group(2)
    impm = IMPID_RE.search(payload)
    if action != "modifyurl" or not impm:
        return [], None, False
    impid = impm.group(1).strip()
    pm = IMPID_PARTS_RE.match(impid)
    if not pm:
        return [], None, False
    date8, nametype = pm.group(1), pm.group(2)
    if nametype.endswith("md"):
        proj_type = "md"
    elif nametype.endswith("a"):
        proj_type = "a"
    else:
        return [], None, False
    ts = f"{date8[:4]}-{date8[4:6]}-{date8[6:8]}"

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

    project_dir = impid.rsplit(".", 1)[0]  # strip trailing .<urlid> -> dir basename
    events = [{"metric": op, "ts": ts, "value": 1, "ext_key": f"{impid}:{op}"}]

    # Status change is OPTIONAL — most lines only touch the archive.
    ls = params.get("livestateselect", "")
    if ls:
        status = STATUS_MAP.get(ls)
        if status is None:
            return [], None, False  # present but unknown code -> trap
        events.append({"metric": status, "ts": ts, "value": 1,
                       "ext_key": f"{impid}:{status}"})
    return events, project_dir, True


# --- token / state / ssh ---------------------------------------------------

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


_SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30", HOST]


def ssh_stream(cmd):
    # errors='replace': iabget.done can contain non-UTF-8 bytes (Latin-1 in URLs).
    p = subprocess.Popen(_SSH + [cmd], stdout=subprocess.PIPE, text=True,
                         errors="replace", bufsize=1)
    for line in p.stdout:
        yield line
    p.wait()
    if p.returncode != 0:
        raise RuntimeError(f"ssh stream failed (rc={p.returncode}): {cmd}")


def local_stream(localdir):
    """Yield lines from every iabget.done under a LOCAL directory tree (sorted,
    so a project's lines stay contiguous for per-project checkpointing)."""
    for root, _dirs, files in sorted(os.walk(localdir)):
        if "iabget.done" in files:
            with open(os.path.join(root, "iabget.done"),
                      encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    yield line


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
                    help="read iabget.done from this LOCAL dir tree (rsync the files "
                         "from rabbit here first) instead of streaming over ssh — far "
                         "faster, since parsing is then pure local disk")
    ap.add_argument("--dry-run", action="store_true",
                    help="read+parse everything and print the metric breakdown, "
                         "but POST nothing and write no state — verify before a hot run")
    args = ap.parse_args()

    token = resolve_token(args)
    if not token and not args.dry_run:
        ap.error("no token (--token, --token-file, ~/.tss_token_medic_iabotdb, "
                 "or $TSS_MEDIC_IABOTDB_TOKEN)")

    done = load_done(args.state)
    unknown = []
    tally = Counter()

    def on_retry(attempt, wait, reason):
        print(f"    {reason}; retry {attempt} in {wait:.0f}s", file=sys.stderr)

    def post(events):
        url = args.api + "/events" + ("?rollup=defer" if args.backfill else "")
        for i in range(0, len(events), args.batch_size):
            tss_http.post_json(url, token, {"events": events[i:i + args.batch_size]},
                               max_retries=tss_http.BATCH_RETRIES, on_retry=on_retry)

    cur = None
    buf = []
    ingested = skipped = total = 0

    def flush():
        nonlocal buf, ingested, skipped, total
        if cur is None or not buf:
            buf = []
            return
        if cur in done:                # already posted in a prior run
            skipped += 1
            buf = []
            return
        if args.dry_run:
            tally.update(e["metric"] for e in buf)
        else:
            try:
                post(buf)
            except (tss_http.FatalHTTP, RuntimeError) as e:
                print(f"\nstopped at {cur}: {e}\n  checkpoint saved "
                      f"({ingested} projects done this run); re-run to resume",
                      file=sys.stderr)
                sys.exit(1)
            done.add(cur)
            save_done(args.state, done)   # checkpoint AFTER this project lands
        ingested += 1
        total += len(buf)
        buf = []

    t0 = time.monotonic()
    nlines = events_seen = 0
    PROGRESS_EVERY = 200000
    src = f"local {args.local_dir}" if args.local_dir else f"ssh {HOST}"
    print(f"[+0s] reading iabget.done from {src} …", file=sys.stderr, flush=True)
    stream = (local_stream(args.local_dir) if args.local_dir
              else ssh_stream("find -L " + METAIMP +
                              " -maxdepth 2 -name iabget.done -type f -exec cat '{}' +"))
    for line in stream:
        nlines += 1
        events, proj, ok = parse_line(line)
        if not ok:
            unknown.append(line.strip())
            continue
        if not events:
            continue
        events_seen += len(events)
        if proj != cur:
            flush()
            cur = proj
        buf.extend(events)
        if nlines % PROGRESS_EVERY == 0:
            el = time.monotonic() - t0
            print(f"[+{el:.0f}s] {nlines:,} lines, {events_seen:,} events, "
                  f"{ingested} projects, {nlines / el:,.0f} lines/s",
                  file=sys.stderr, flush=True)
    flush()

    mode = "DRY RUN" if args.dry_run else ("backfill" if args.backfill else "poll")
    print(f"{mode}: {ingested} projects ({total} events), {skipped} already-done, "
          f"{len(unknown)} trapped lines")
    if args.dry_run:
        print("events by metric (NOTHING posted):")
        for metric, n in sorted(tally.items()):
            print(f"  {metric:18} {n:>12,}")
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
