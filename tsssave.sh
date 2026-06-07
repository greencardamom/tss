#!/bin/bash
#
# tsssave.sh - one-shot deploy for the tss tool (Tarb Stats Server).
#
#   1. commit local changes   (in /home/greenc/repos/gh/tss)
#   2. push to GitHub (origin/main)
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

REPO="/home/greenc/repos/gh/tss"
cd "$REPO"

PUSHONLY=0
if [ "${1:-}" = "--pushonly" ]; then
  PUSHONLY=1
  shift
fi

MSG="${*:-tss update $(date '+%Y-%m-%d %H:%M:%S')}"

echo "==> commit"
if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git commit -m "$MSG"
else
  echo "    (no local changes to commit)"
fi

echo "==> push to GitHub"
git push origin main

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
