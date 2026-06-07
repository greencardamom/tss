#!/usr/bin/env python3
"""backfill_iabw.py - load historical IABotWatch data into TSS.

Reads the legacy IABW day files (db/YYYY/NNN.txt, one line per revision:
`wiki revid c3 c4 c5 c6 c7 c8`) and POSTs them to TSS as events for the
'iabotwatch' source, with rollups DEFERRED. After all events are loaded, rebuild
rollups once on Toolforge -- as a python3.11 JOB (the venv is 3.11; the bastion
is 3.13, so running the venv python on the bastion fails to import pymysql):

    toolforge jobs run rebuild-iabw --image python3.11 --mount all --wait \
      --command '$HOME/www/python/venv/bin/python $HOME/www/python/src/rebuild_rollups.py iabotwatch'

Designed to run on acre, where the db files live. Stdlib only (no requests).
HTTP goes through tss_http (the shared hardened retry/backoff helper, ported from
bup's wiki.py). Safe to re-run: events are idempotent (ext_key) and a progress
file lets it resume after an interruption.

Usage:
    TSS_TOKEN=xxxx ./backfill_iabw.py
    TSS_TOKEN=xxxx ./backfill_iabw.py --years 2020-2024 --batch-size 1000
    ./backfill_iabw.py --dry-run            # parse + count only, no POST
"""
import argparse
import json
import os
import sys

import iabw_parse
import tss_http
import tss_token

API_DEFAULT = "https://tss.toolforge.org/api/v1"


# --- HTTP ------------------------------------------------------------------

def _on_retry(attempt, wait, reason):
    print(f"    {reason}; retry {attempt} in {wait:.0f}s", file=sys.stderr)


def post_events(api, token, events):
    """POST one batch with rollups deferred, via the shared hardened helper."""
    url = f"{api}/events?rollup=defer"
    return tss_http.post_json(
        url, token, {"events": events},
        max_retries=tss_http.BATCH_RETRIES, on_retry=_on_retry,
    )


# --- progress (resume) -----------------------------------------------------

def load_state(path):
    if path and os.path.exists(path):
        with open(path) as fh:
            return set(json.load(fh).get("done", []))
    return set()


def save_state(path, done):
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"done": sorted(done)}, fh)
    os.replace(tmp, path)


# --- main ------------------------------------------------------------------

def parse_years(spec):
    if not spec:
        return None  # auto-detect
    out = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def main():
    ap = argparse.ArgumentParser(description="Backfill IABotWatch data into TSS.")
    ap.add_argument("--db-dir",
                    default="/home/greenc/toolforge/iabotwatch/www/db",
                    help="directory containing YYYY/NNN.txt day files")
    ap.add_argument("--api", default=API_DEFAULT, help="TSS API base URL")
    ap.add_argument("--token", help="iabotwatch write token")
    ap.add_argument("--token-file", help="file containing the write token")
    ap.add_argument("--years", help="e.g. 2020-2024 or 2021,2023 (default: all found)")
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument("--state",
                    default=os.path.expanduser("~/.tss_backfill_iabw.state"),
                    help="progress file for resume (empty string to disable)")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and count only; do not POST")
    args = ap.parse_args()

    # --token, then --token-file, then $TSS_TOKEN, then ~/.tss_token
    token = tss_token.resolve(args.token, args.token_file)
    if not args.dry_run and not token:
        ap.error("no write token (--token, --token-file, $TSS_TOKEN, or "
                 "~/.tss_token); or use --dry-run")

    years = parse_years(args.years)
    if years is None:
        years = sorted(
            int(d) for d in os.listdir(args.db_dir)
            if d.isdigit() and os.path.isdir(os.path.join(args.db_dir, d))
        )
    if not years:
        print(f"no year directories found under {args.db_dir}", file=sys.stderr)
        sys.exit(1)

    state_path = args.state or None
    done = load_state(state_path)

    total_files = total_events = skipped = 0
    print(f"backfill: db={args.db_dir} years={years[0]}-{years[-1]} "
          f"api={args.api} dry_run={args.dry_run}")

    for year, doy, path in iabw_parse.iter_day_files(args.db_dir, years):
        key = f"{year}/{os.path.basename(path)}"
        if key in done:
            skipped += 1
            continue
        date_iso = iabw_parse.doy_to_date(year, doy).isoformat()
        events = iabw_parse.parse_day_file(path, date_iso)
        total_files += 1
        total_events += len(events)

        if args.dry_run:
            print(f"  {key} -> {date_iso}: {len(events)} events")
            continue

        try:
            for i in range(0, len(events), args.batch_size):
                post_events(args.api, token, events[i:i + args.batch_size])
        except tss_http.FatalHTTP as e:
            # Non-retryable (bad token / unknown metric / malformed): stop, don't
            # mark this file done so a fixed re-run resumes here (idempotent).
            save_state(state_path, done)
            print(f"\nFATAL at {key}: {e}", file=sys.stderr)
            print("  Check the write token and that sql/seed.sql loaded the "
                  "iabotwatch metrics.", file=sys.stderr)
            sys.exit(1)
        except RuntimeError as e:
            save_state(state_path, done)
            print(f"\nGAVE UP at {key}: {e}", file=sys.stderr)
            print("  Progress saved; re-run to resume from here.", file=sys.stderr)
            sys.exit(1)
        done.add(key)
        save_state(state_path, done)
        print(f"  {key} -> {date_iso}: {len(events)} events sent")

    print(f"\nfiles processed: {total_files} (skipped {skipped} already done), "
          f"events: {total_events}")
    if not args.dry_run:
        print("\nNext: rebuild rollups as a python3.11 job ON TOOLFORGE:")
        print("  toolforge jobs run rebuild-iabw --image python3.11 --mount all --wait \\")
        print("    --command '$HOME/www/python/venv/bin/python "
              "$HOME/www/python/src/rebuild_rollups.py iabotwatch'")


if __name__ == "__main__":
    main()
