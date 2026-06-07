#!/usr/bin/env python3
"""pull_booksup.py - pull BooksUp daily stats into TSS.

BooksUp publishes one JSONL file per year (one line per UTC day), recomputed by
its own daily job. We read that file (preferring the local Toolforge path, with
an HTTP fallback), turn each day into TSS events for the 'booksup' source, POST
them with rollups deferred, then trigger a (fast, tiny) rollup rebuild.

Daily granularity is the right cadence: BooksUp's gadget metric is only knowable
from its once-daily enwiki replica sweep, and TSS's finest resolution is Day --
so there is nothing to gain from event-driven pushing.

Re-running is safe: events are idempotent (ext_key = "<date>:<metric>"), so
re-pulling a day -- or a day BooksUp regenerated with `stats.py --date` -- just
overwrites in place. Zero-valued days are sent too, so a real zero is
distinguishable from "no data".

Designed to run as a daily Toolforge job in the tss tool. Stdlib only.

Usage:
  ./pull_booksup.py                      # current + previous year
  ./pull_booksup.py --years 2026
  ./pull_booksup.py --dry-run            # parse + count only, no POST
"""
import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.request

import tss_http

API_DEFAULT = "https://tss.toolforge.org/api/v1"
SRC_DIR_DEFAULT = "/data/project/bup/www/static"
URL_BASE_DEFAULT = "https://tools-static.wmflabs.org/bup"
TOKEN_FILE_DEFAULT = os.path.expanduser("~/.tss_token_booksup")

# BooksUp record group.field -> TSS metric slug. (urls_added is derived =
# webtool.urls + gadget.urls, so it is NOT stored; computed on read instead.)
FIELD_MAP = [
    ("webtool", "edits", "webtool_edits"),
    ("webtool", "urls",  "webtool_urls"),
    ("gadget",  "edits", "gadget_edits"),
    ("gadget",  "urls",  "gadget_urls"),
    ("api",     "page",     "api_page"),
    ("api",     "random",   "api_random"),
    ("api",     "worklist", "api_worklist"),
    ("api",     "pages",    "api_pages"),
]


def resolve_token(args):
    """booksup token only -- never fall back to another source's token."""
    if args.token:
        return args.token
    path = args.token_file or TOKEN_FILE_DEFAULT
    if os.path.exists(path):
        tok = open(path).read().strip()
        if tok:
            return tok
    return os.environ.get("TSS_BOOKSUP_TOKEN")


def read_year(year, src_dir, url_base):
    """Return (lines, source_label). Prefer the local file; fall back to HTTP."""
    name = f"booksup-stats-{year}.jsonl"
    local = os.path.join(src_dir, name)
    if os.path.exists(local):
        with open(local, encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines(), local
    url = url_base.rstrip("/") + "/" + name
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            return resp.read().decode("utf-8", "replace").splitlines(), url
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return [], url  # no file for that year yet
        raise
    except urllib.error.URLError:
        return [], url


def record_to_events(rec):
    """Map one BooksUp day record to TSS events (all 8 metrics, incl. zeros)."""
    date = rec.get("date")
    if not date:
        return []
    events = []
    for group, field, slug in FIELD_MAP:
        try:
            value = int((rec.get(group) or {}).get(field, 0))
        except (TypeError, ValueError):
            value = 0
        events.append({
            "metric": slug,
            "ts": date,
            "value": value,
            "ext_key": f"{date}:{slug}",
        })
    return events


def parse_years(spec):
    if not spec:
        y = datetime.date.today().year
        return [y - 1, y]
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
    ap = argparse.ArgumentParser(description="Pull BooksUp daily stats into TSS.")
    ap.add_argument("--src-dir", default=SRC_DIR_DEFAULT,
                    help="local dir with booksup-stats-<year>.jsonl")
    ap.add_argument("--url-base", default=URL_BASE_DEFAULT,
                    help="HTTP fallback base URL")
    ap.add_argument("--api", default=API_DEFAULT)
    ap.add_argument("--token")
    ap.add_argument("--token-file")
    ap.add_argument("--years", help="e.g. 2026 or 2025,2026 (default current+prev)")
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    events, days, skipped = [], 0, 0
    found_file = False
    for year in parse_years(args.years):
        lines, src = read_year(year, args.src_dir, args.url_base)
        if lines:
            found_file = True
            print(f"read {len(lines)} day(s) from {src}")
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            ev = record_to_events(rec)
            if ev:
                events.extend(ev)
                days += 1

    print(f"parsed {days} day(s) -> {len(events)} events"
          + (f" (skipped {skipped} malformed line(s))" if skipped else ""))

    # No source file found at all (current+previous year) => BooksUp's publishing
    # is broken/missing. Exit non-zero so the job's --emails onfailure alerts.
    if not found_file:
        print("ALERT: no BooksUp stats file found (local or URL) for "
              f"{args.years or 'current+previous year'} — is BooksUp publishing?",
              file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        return
    if not events:
        return

    token = resolve_token(args)
    if not token:
        ap.error("no booksup token (--token, --token-file, ~/.tss_token_booksup, "
                 "or $TSS_BOOKSUP_TOKEN)")

    # Load deferred, then one fast set-based rebuild (booksup data is tiny).
    try:
        for i in range(0, len(events), args.batch_size):
            tss_http.post_json(
                f"{args.api}/events?rollup=defer", token,
                {"events": events[i:i + args.batch_size]},
                max_retries=tss_http.BATCH_RETRIES, on_retry=_on_retry,
            )
        tss_http.post_json(
            f"{args.api}/sources/booksup/rebuild-rollups", token, {},
            max_retries=tss_http.BATCH_RETRIES, on_retry=_on_retry,
        )
    except tss_http.FatalHTTP as e:
        print(f"\nFATAL: {e}\n  Check the booksup token and that seed.sql loaded "
              "the booksup metrics.", file=sys.stderr)
        sys.exit(1)

    print(f"uploaded {len(events)} events and rebuilt booksup rollups")


if __name__ == "__main__":
    main()
