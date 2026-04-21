#!/usr/bin/env bash
# Auto-pull the jade-infra repo and send a Pushover nudge when code changed.
#
# Runs every 5 minutes via cron. Uses `git pull --ff-only` so a divergence
# (e.g., unpushed local commits) aborts cleanly instead of rewriting history.
#
# Silent on no-op pulls. Logs all activity to /root/docker/logs/git-autopull.log.
# Sends a Pushover nudge ONLY when the pull changed non-docs files, so
# docs-only edits on the laptop don't wake your phone.

set -euo pipefail

REPO="/opt/jade-infra"
LOG="/root/docker/logs/git-autopull.log"
CONFIG="${REPO}/scripts/config.env"

# Load Pushover creds (PUSHOVER_APP_TOKEN, PUSHOVER_USER_KEY)
# shellcheck source=/dev/null
source "$CONFIG"

cd "$REPO"

OLD_HEAD=$(git rev-parse HEAD)

if ! pull_output=$(git pull --ff-only 2>&1); then
  {
    echo "[$(date -Iseconds)] PULL FAILED"
    echo "$pull_output" | sed 's/^/  /'
  } >> "$LOG"
  exit 1
fi

NEW_HEAD=$(git rev-parse HEAD)
if [ "$OLD_HEAD" = "$NEW_HEAD" ]; then
  # no-op, silent
  exit 0
fi

CHANGED=$(git diff --name-only "$OLD_HEAD" "$NEW_HEAD")

# Classify: non-docs change requires deploy action
NEEDS_DEPLOY=0
if echo "$CHANGED" | grep -qvE '^(docs/|README\.md$|\.gitignore$)'; then
  NEEDS_DEPLOY=1
fi

{
  echo "[$(date -Iseconds)] Pulled ${OLD_HEAD:0:7} → ${NEW_HEAD:0:7} (needs_deploy=$NEEDS_DEPLOY)"
  echo "$CHANGED" | sed 's/^/  /'
} >> "$LOG"

if [ "$NEEDS_DEPLOY" = "1" ]; then
  COMMIT_MSG=$(git log "$OLD_HEAD..$NEW_HEAD" --pretty=format:'- %s' | head -5)
  FILE_COUNT=$(echo "$CHANGED" | wc -l)
  MSG="$FILE_COUNT file(s) changed incl. code/config — SSH in to deploy.

Recent commits:
$COMMIT_MSG"
  curl -fsS https://api.pushover.net/1/messages.json \
    -F "token=${PUSHOVER_APP_TOKEN}" \
    -F "user=${PUSHOVER_USER_KEY}" \
    -F "title=jade-infra: deploy needed" \
    -F "message=${MSG}" \
    -F "priority=0" >/dev/null && echo "  → Pushover sent" >> "$LOG" || echo "  → Pushover FAILED" >> "$LOG"
fi
