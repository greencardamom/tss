#!/bin/bash
#
# sync_medic_iabotdb.sh - ongoing daily sync of WaybackMedic's IABot-DB work into TSS.
#
# Runs on ACRE (cron). Two steps:
#   1. rsync the iabget.done files from RABBIT's LOCAL disk to /beater (incremental,
#      so only new/changed files transfer). Reads rabbit directly, NOT via the sheep
#      VM's slow shared-folder NFS.
#   2. ingest with pull_medic_iabotdb.py --local-dir (live rollups). The per-project
#      done-set skips projects already loaded, so only newly-COMPLETED projects post.
#      (A project has no iabget.done until medic finishes it, 3-48h later, so it's
#      simply picked up on a later run.)
#
# Idempotent + crash-safe (re-running re-reads local disk and resends nothing).
# crontab (acre):
#   30 3 * * * /home/greenc/repos/gh/tss/loaders/sync_medic_iabotdb.sh >> /home/greenc/medic_iabotdb.log 2>&1
set -euo pipefail

DEST=/beater/medic_metaimp
REPO=/home/greenc/repos/gh/tss
RABBIT_METAIMP=/home/greenc/sharedNFS/medic/metaimp

mkdir -p "$DEST"
echo "=== $(date '+%F %T') sync_medic: rsync from rabbit ==="
rsync -rt --prune-empty-dirs \
  --include='*/' --include='iabget.done' --exclude='*' \
  -e 'ssh -o BatchMode=yes -o ConnectTimeout=30' \
  "rabbit:$RABBIT_METAIMP/" "$DEST/"

echo "=== $(date '+%F %T') sync_medic: ingest new projects ==="
"$REPO/loaders/pull_medic_iabotdb.py" --local-dir "$DEST"
echo "=== $(date '+%F %T') sync_medic: done ==="
