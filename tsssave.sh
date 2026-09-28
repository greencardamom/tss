#!/bin/bash
#
# tsssave.sh - one-shot deploy for the tss tool (Tarb Stats Server).
#
#   1. copy this staging dir to acre (~/scripts/push_staging_repos.sh tss)
#   2. acre commits and pushes to GitHub (origin/main) - acre is the only host that pushes
#   3. git pull on Toolforge  (/data/project/tss/www, via deploy key)
#   4. webservice restart
#
# Usage:
#   ./tsssave.sh                  # commit everything + push + deploy (auto msg)
#   ./tsssave.sh "fix series"     # ... with that commit message
#   ./tsssave.sh --pushonly       # commit + push to GitHub ONLY (no Toolforge
#   ./tsssave.sh --pushonly "msg" #     pull/restart) -- e.g. docs-only changes
#
set -euo pipefail

PUSHONLY=0
if [ "${1:-}" = "--pushonly" ]; then
  PUSHONLY=1
  shift
fi

MSG="${*:-tss update $(date '+%Y-%m-%d %H:%M:%S')}"

echo "==> copy to acre"
/home/greenc/scripts/push_staging_repos.sh tss

echo "==> commit + push to GitHub (on acre)"
{ printf 'MSG=%q\n' "$MSG"; cat <<'ACRE'
set -e
cd /home/greenc/repos/gh/tss
git add -A
if git diff --cached --quiet; then echo "    (no changes to commit)"; else git commit -q -m "$MSG"; fi
git push -q origin main
ACRE
} | ssh -o BatchMode=yes -o ConnectTimeout=15 acre bash -s

if [ "$PUSHONLY" -eq 1 ]; then
  echo "==> --pushonly: skipping Toolforge pull/restart"
  echo "==> done"
  exit 0
fi

echo "==> pull + restart on Toolforge"
ssh -o BatchMode=yes -o ConnectTimeout=30 tools 'become tss bash -s' <<'REMOTE'
set -e
cd /data/project/tss/www
git pull --ff-only origin main
# restart if already running; otherwise bring it up for the first time
webservice restart || webservice python3.11 start
REMOTE

echo "==> done"
