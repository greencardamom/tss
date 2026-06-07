#!/usr/bin/env python3
"""pull_medic_iabotdb.py - WaybackMedic's IABot-DB work -> TSS (source 'medic_iabotdb').

Runs on ACRE; pulls medic's iabget.done logs from `sheep` over ssh and turns each
logged IABot-DB update into TSS events. Global (no entity); daily buckets by the
project-name date embedded in each line's IMPID.

Per iabget.done line (one update) -> TWO events:
  archive op : archive_add | archive_modify | archive_delete | status_only
  status set : set_dead(0) set_alive(3) set_paywall(5) set_permadead(6) set_permalive(7)
Lines that aren't `-a modifyurl`, or won't parse, are TRAPPED (logged + non-zero
exit) so odd historic formats surface instead of being silently dropped.

A medic project takes 3-48h, so a project directory existing does NOT mean it has
finished. The ongoing poll tracks each project as:
  done       - iabget.done found + ingested (terminal)
  pending    - dir exists, no iabget.done yet -> retry next run (tries counter)
  abandoned  - gave up after --max-tries (default 3 ~= 3 daily polls) (terminal)

Modes:
  --backfill   stream ALL completed projects once (deferred rollups); then rebuild:
                 rebuild_rollups.py medic_iabotdb   (as a python3.11 job on Toolforge)
  (default)    poll: process not-yet-terminal project dirs; ingest finished ones
               (live rollups), advance/abandon the unfinished. For the acre cron.

Idempotent (ext_key = "<IMPID>:<metric>"). sheep runs tcsh -> remote commands are
tcsh-safe (no $(), no 2>). Token: --token / --token-file / ~/.tss_token_medic_iabotdb
/ $TSS_MEDIC_IABOTDB_TOKEN. Stdlib only.
"""
import argparse
import json
import os
import re
import subprocess
import sys

import tss_http

SHEEP = "sheep"
METAIMP = "~/wm/metaimp"
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
    """-> (events, project_dir, ok).

    ok=False means the line is unparseable / not modifyurl -> caller traps it.
    events=[] with ok=True means a blank line.
    """
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
    status = STATUS_MAP.get(params.get("livestateselect", ""))
    if status is None:
        return [], None, False  # unknown/absent status code -> trap

    archiveurl = params.get("archiveurl", "")
    if archiveurl == "":
        op = "archive_delete" if proj_type == "md" else "status_only"
    elif proj_type == "a":
        op = "archive_add"
    else:  # md, archive present
        op = "status_only" if archiveurl == old_o else "archive_modify"

    project_dir = impid.rsplit(".", 1)[0]  # strip trailing .<urlid> -> dir basename
    events = [
        {"metric": op, "ts": ts, "value": 1, "ext_key": f"{impid}:{op}"},
        {"metric": status, "ts": ts, "value": 1, "ext_key": f"{impid}:{status}"},
    ]
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


def load_state(path):
    if path and os.path.exists(path):
        return json.load(open(path)).get("projects", {})
    return {}


def save_state(path, projects):
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"projects": projects}, fh)
    os.replace(tmp, path)


_SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30", SHEEP]


def ssh_run(cmd):
    # errors='replace': iabget.done can contain non-UTF-8 bytes (Latin-1 in URLs).
    p = subprocess.run(_SSH + [cmd], capture_output=True, text=True, errors="replace")
    return p.returncode, p.stdout, p.stderr


def ssh_stream(cmd):
    p = subprocess.Popen(_SSH + [cmd], stdout=subprocess.PIPE, text=True,
                         errors="replace", bufsize=1)
    for line in p.stdout:
        yield line
    p.wait()
    if p.returncode != 0:
        raise RuntimeError(f"ssh stream failed (rc={p.returncode}): {cmd}")


def list_project_dirs():
    # find (not a glob) avoids ARG_MAX + tcsh 'No match'; -printf gives basenames.
    # -L: ~/wm/metaimp is a symlink (-> sharedNFS), so find must follow it.
    rc, out, err = ssh_run("find -L " + METAIMP +
                           " -maxdepth 1 -type d -name 'imp*' -printf '%f\\n'")
    if rc != 0:
        raise RuntimeError("list dirs failed: " + err.strip()[:200])
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def fetch_iabget_done(base):
    """Return list of lines, or None if iabget.done is missing/empty (not finished)."""
    rc, out, err = ssh_run("cat " + METAIMP + "/" + base + "/iabget.done")
    if rc != 0 or not out.strip():
        return None
    return out.splitlines()


# --- main ------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="WaybackMedic IABot-DB work -> TSS.")
    ap.add_argument("--backfill", action="store_true",
                    help="stream all completed projects once, rollups deferred")
    ap.add_argument("--api", default=API_DEFAULT)
    ap.add_argument("--token")
    ap.add_argument("--token-file")
    ap.add_argument("--max-tries", type=int, default=3,
                    help="give up on a project missing iabget.done after N polls")
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument("--state", default=STATE_DEFAULT)
    args = ap.parse_args()

    token = resolve_token(args)
    if not token:
        ap.error("no token (--token, --token-file, ~/.tss_token_medic_iabotdb, "
                 "or $TSS_MEDIC_IABOTDB_TOKEN)")

    projects = load_state(args.state)
    unknown = []  # trapped raw lines

    def on_retry(attempt, wait, reason):
        print(f"    {reason}; retry {attempt} in {wait:.0f}s", file=sys.stderr)

    def post(events, defer):
        if not events:
            return
        url = args.api + "/events" + ("?rollup=defer" if defer else "")
        for i in range(0, len(events), args.batch_size):
            tss_http.post_json(url, token, {"events": events[i:i + args.batch_size]},
                               max_retries=tss_http.BATCH_RETRIES, on_retry=on_retry)

    if args.backfill:
        buf, seen, total = [], set(), 0
        for line in ssh_stream("find -L " + METAIMP +
                               " -maxdepth 2 -name iabget.done -type f -exec cat '{}' +"):
            events, proj, ok = parse_line(line)
            if not ok:
                unknown.append(line.strip())
                continue
            if not events:
                continue
            seen.add(proj)
            buf.extend(events)
            total += len(events)
            if len(buf) >= args.batch_size:
                post(buf, defer=True)
                buf = []
        post(buf, defer=True)
        for proj in seen:
            projects[proj] = {"status": "done"}
        save_state(args.state, projects)
        print(f"backfill: {len(seen)} projects, {total} events (deferred)")
        if total:
            print("Next: rebuild rollups as a python3.11 job on Toolforge:")
            print("  toolforge jobs run rebuild-medic --image python3.11 --mount all --wait \\")
            print("    --command '$HOME/www/python/venv/bin/python "
                  "$HOME/www/python/src/rebuild_rollups.py medic_iabotdb'")
    else:
        dirs = list_project_dirs()
        done = pending = abandoned = 0
        for base in dirs:
            status = projects.get(base, {}).get("status")
            if status in ("done", "abandoned"):
                continue
            lines = fetch_iabget_done(base)
            if lines is None:
                tries = projects.get(base, {}).get("tries", 0) + 1
                if tries >= args.max_tries:
                    projects[base] = {"status": "abandoned", "tries": tries}
                    print(f"  GAVE UP after {tries} polls (no iabget.done): {base}",
                          file=sys.stderr)
                    abandoned += 1
                else:
                    projects[base] = {"status": "pending", "tries": tries}
                    pending += 1
                save_state(args.state, projects)
                continue
            buf = []
            for line in lines:
                events, _proj, ok = parse_line(line)
                if not ok:
                    unknown.append(line.strip())
                    continue
                buf.extend(events)
            post(buf, defer=False)  # live rollups
            projects[base] = {"status": "done"}
            save_state(args.state, projects)
            done += 1
        print(f"poll: {done} ingested, {pending} pending, {abandoned} abandoned, "
              f"of {len(dirs)} dirs")

    if unknown:
        print(f"\nTRAP: {len(unknown)} unparsed/non-modifyurl line(s) — surfacing:",
              file=sys.stderr)
        for s in unknown[:20]:
            print("  " + s[:200], file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
