# -*- coding: utf-8 -*-
#
# Parsing of the legacy IABotWatch db files into TSS events. Shared by the
# one-time backfill loader and the live outbox uploader, so both interpret the
# format identically (no drift).
#
# Day file: db/YYYY/NNN.txt, one line per revision:
#   wiki revid c3 [c4 [c5 [c6 [c7 [c8]]]]]
# The counter columns were appended over the years (lines may have 3..8 fields,
# never reordered), so counters map POSITIONALLY onto METRICS and trailing
# metrics are simply absent for older data.

import datetime
import os
import re

# Counter column order (db fields 3..8) -> TSS metric slug.
METRICS = [
    "iabot_wayback",     # field 3: Wayback URLs added by IABot
    "iabot_details",     # field 4: archive.org/details by an IA bot
    "other_details",     # field 5: archive.org/details by other means
    "user_wayback",      # field 6: Wayback URLs added by Users
    "otherbot_wayback",  # field 7: Wayback URLs added by other bots
    "iabot_sim",         # field 8: sim_ books added by IABot
]

DAY_FILE_RE = re.compile(r"^(\d{3})\.txt$")  # NNN.txt only (not .details/.italic/...)


def doy_to_date(year, doy):
    return datetime.date(year, 1, 1) + datetime.timedelta(days=doy - 1)


def parse_lines(lines, date_iso):
    """Return TSS event dicts for an iterable of db rows, all dated `date_iso`."""
    events = []
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue  # need wiki, revid, and at least one counter
        wiki, revid = parts[0], parts[1]
        for i, raw in enumerate(parts[2:2 + len(METRICS)]):
            try:
                v = int(raw)
            except ValueError:
                continue
            if v <= 0:
                continue
            slug = METRICS[i]
            events.append({
                "metric": slug,
                "entity": wiki,
                "ts": date_iso,
                "value": v,
                "ref_id": revid,
                "ext_key": f"{wiki}:{revid}:{slug}",
            })
    return events


def parse_day_file(path, date_iso):
    """Return TSS event dicts for one whole NNN.txt file."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        return parse_lines(fh, date_iso)


def iter_day_files(db_dir, years):
    """Yield (year, doy, path) for every NNN.txt under db_dir/<year>/, sorted."""
    for year in years:
        ydir = os.path.join(db_dir, str(year))
        if not os.path.isdir(ydir):
            continue
        for name in sorted(os.listdir(ydir)):
            m = DAY_FILE_RE.match(name)
            if m:
                yield year, int(m.group(1)), os.path.join(ydir, name)
