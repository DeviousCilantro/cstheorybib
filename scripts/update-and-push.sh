#!/usr/bin/env bash
set -euo pipefail

# Set these two variables for your server.
REPO="/absolute/path/to/theorybib"
CONTACT="your.email@example.edu"

LOCK="${REPO}/meta/cron.lock"
LOG_PREFIX="[$(date -Is)]"

cd "$REPO"
mkdir -p meta

# flock prevents overlapping cron runs. It exits immediately if another run is active.
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$LOG_PREFIX another update is already running"
  exit 0
fi

python3 gen.py \
  --engine rust \
  --contact "$CONTACT" \
  --delay 10 \
  --jitter 3 \
  --cooldown 900 \
  --max-retries 12

if git diff --quiet -- bib meta; then
  echo "$LOG_PREFIX no bibliography changes"
  exit 0
fi

git add bib meta
git commit -m "Update DBLP bibliography $(date -I)"
git push
