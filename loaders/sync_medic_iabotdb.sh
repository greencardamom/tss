#!/bin/bash
#
# sync_medic_iabotdb.sh - ongoing daily sync of WaybackMedic's IABot-DB work into TSS.
#
# Runs on ACRE (cron). Steps:
#   1. rsync the iabget.done files from RABBIT's LOCAL disk into a scratch dir on
#      /beater. Reads rabbit directly (~35 MB/s), NOT via the sheep VM's slow
#      shared-folder NFS. Full pull each run (~1 min) — cheap, and avoids leaving a
#      stale 1.9 GB mirror lying around.
#   2. ingest with pull_medic_iabotdb.py --local-dir (live rollups). The per-project
#      done-set in ~/.config/tss/medic_iabotdb.state (NOT on /beater) skips projects already
#      loaded, so only newly-COMPLETED projects post. A project has no iabget.done
#      until medic finishes it (3-48h), so it's simply picked up on a later run.
#   3. clean up: the scratch dir is removed on exit (success OR failure), so /beater
#      is always left clean.
#
# Idempotent + crash-safe (re-running re-pulls + resends nothing).
# crontab (acre, tcsh redirect):
#   30 3 * * * /home/greenc/repos/gh/tss/loaders/sync_medic_iabotdb.sh >>& /home/greenc/medic_iabotdb.log
set -euo pipefail

DEST=/beater/medic_metaimp
REPO=/home/greenc/repos/gh/tss
RABBIT_METAIMP=/home/greenc/sharedNFS/medic/metaimp

cleanup() { rm -rf "$DEST"; }
trap cleanup EXIT                 # always leave /beater clean, even on error

rm -rf "$DEST"; mkdir -p "$DEST"  # start clean (also clears any killed-run leftover)
echo "=== $(date '+%F %T') sync_medic: rsync from rabbit ==="
rsync -rt --prune-empty-dirs \
  --include='*/' --include='iabget.done' --include='iabget.orig' --exclude='*' \
  -e 'ssh -o BatchMode=yes -o ConnectTimeout=30' \
  "rabbit:$RABBIT_METAIMP/" "$DEST/"

echo "=== $(date '+%F %T') sync_medic: ingest new projects ==="
"$REPO/loaders/pull_medic_iabotdb.py" --local-dir "$DEST"
echo "=== $(date '+%F %T') sync_medic: done (removing scratch $DEST) ==="
