#!/usr/bin/env python3
"""upload_outbox_iabw.py - drain new IABotWatch rows to TSS (the live path).

transform.awk appends rows to db/YYYY/NNN.txt as the EventStream is processed.
This uploader tails each day file from a saved byte offset, turns the new rows
into TSS events, and POSTs them (rollups updated live). On success it advances
the offset. It is the durable buffer the design called for: if TSS is down,
offsets do not advance, the rows sit safely in the db files, and the next run
resends them. Idempotent (ext_key), so a partial/retried send never doubles.

Run once per invocation; schedule it from cron (every ~15 min, after cron-run):
  */15 * * * * /home/greenc/repos/gh/tss/loaders/upload_outbox_iabw.py >> ~/tss_upload.log 2>&1

First time, AFTER the historical backfill, seed the offsets so it only tails new
rows instead of re-sending the year:
  ./upload_outbox_iabw.py --init

Token resolution: --token, then --token-file, then $TSS_TOKEN, then ~/.config/tss/token.
Stdlib only; HTTP via the shared hardened tss_http helper.
"""
import argparse
import atexit
import datetime
import json
import os
import sys

import iabw_parse
import tss_http
import tss_token

API_DEFAULT = "https://tss.toolforge.org/api/v1"
STATE_DEFAULT = os.path.expanduser("~/.config/tss/outbox_iabw.state")
LOCK_DEFAULT = os.path.expanduser("~/.config/tss/outbox_iabw.lock")


# --- state / lock ----------------------------------------------------------

def load_offsets(path):
    if path and os.path.exists(path):
        with open(path) as fh:
            return json.load(fh).get("offsets", {})
    return {}


def save_offsets(path, offsets):
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"offsets": offsets}, fh)
    os.replace(tmp, path)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def acquire_lock(path):
    """True if we got the lock. Honors a live previous run; clears a stale one."""
    if os.path.exists(path):
        try:
            old = int(open(path).read().strip())
        except (ValueError, OSError):
            old = None
        if old and _pid_alive(old):
            return False  # another run is active
    with open(path, "w") as fh:
        fh.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(path) and os.remove(path))
    return True


# --- tailing ---------------------------------------------------------------

def tail_lines(path, start):
    """Read new complete lines from byte `start`.

    Returns (lines, new_offset). Only data up to the last newline is consumed, so
    a line transform.awk is mid-writing is left for the next run (never split).
    """
    size = os.path.getsize(path)
    if size <= start:
        return [], start
    with open(path, "rb") as fh:
        fh.seek(start)
        chunk = fh.read(size - start)
    nl = chunk.rfind(b"\n")
    if nl < 0:
        return [], start  # no complete line yet
    consumed = chunk[:nl + 1]
    lines = consumed.decode("utf-8", "replace").splitlines()
    return lines, start + len(consumed)


# --- main ------------------------------------------------------------------

def default_years():
    """Current year plus the previous one (covers Jan rollover + late events)."""
    y = datetime.date.today().year
    return [y - 1, y]


def parse_years(spec):
    if not spec:
        return default_years()
    out = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def _on_retry(attempt, wait, reason):
    print(f"    {reason}; retry {attempt} in {wait:.0f}s", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Drain new IABotWatch rows to TSS.")
    ap.add_argument("--db-dir", default="/home/greenc/toolforge/iabotwatch/www/db")
    ap.add_argument("--api", default=API_DEFAULT)
    ap.add_argument("--token")
    ap.add_argument("--token-file")
    ap.add_argument("--years", help="override (e.g. 2025,2026); default current+prev")
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument("--state", default=STATE_DEFAULT)
    ap.add_argument("--lock", default=LOCK_DEFAULT)
    ap.add_argument("--init", action="store_true",
                    help="seed offsets to current file sizes (mark existing rows "
                         "as already sent) and exit; run once after backfill")
    args = ap.parse_args()

    years = parse_years(args.years)
    state_path = args.state or None
    offsets = load_offsets(state_path)

    if not acquire_lock(args.lock):
        print("another upload run is active; exiting", file=sys.stderr)
        return

    # --init: record where each file currently ends, upload nothing.
    if args.init:
        n = 0
        for year, doy, path in iabw_parse.iter_day_files(args.db_dir, years):
            offsets[f"{year}/{os.path.basename(path)}"] = os.path.getsize(path)
            n += 1
        save_offsets(state_path, offsets)
        print(f"init: seeded offsets for {n} files; uploads will tail new rows")
        return

    token = tss_token.resolve(args.token, args.token_file)
    if not token:
        ap.error("no write token (--token, --token-file, $TSS_TOKEN, or ~/.config/tss/token)")

    url = f"{args.api}/events"  # live path: rollups updated (no defer)
    sent_files = sent_events = 0

    for year, doy, path in iabw_parse.iter_day_files(args.db_dir, years):
        key = f"{year}/{os.path.basename(path)}"
        start = offsets.get(key, 0)
        lines, new_off = tail_lines(path, start)
        if not lines:
            continue
        events = iabw_parse.parse_lines(lines, iabw_parse.doy_to_date(year, doy).isoformat())
        if not events:
            offsets[key] = new_off          # rows had only zero counters; skip ahead
            save_offsets(state_path, offsets)
            continue

        try:
            for i in range(0, len(events), args.batch_size):
                tss_http.post_json(
                    url, token, {"events": events[i:i + args.batch_size]},
                    max_retries=tss_http.BATCH_RETRIES, on_retry=_on_retry,
                )
        except tss_http.FatalHTTP as e:
            print(f"\nFATAL at {key}: {e}", file=sys.stderr)
            print("  Offset NOT advanced; check token/metrics, then re-run.",
                  file=sys.stderr)
            sys.exit(1)
        except RuntimeError as e:
            print(f"\nGAVE UP at {key}: {e}  (offset not advanced; resends next run)",
                  file=sys.stderr)
            sys.exit(1)

        offsets[key] = new_off              # advance only after the whole delta lands
        save_offsets(state_path, offsets)
        sent_files += 1
        sent_events += len(events)

    if sent_events:
        print(f"uploaded {sent_events} events from {sent_files} file(s)")


if __name__ == "__main__":
    main()
