#!/usr/bin/env bash
# Install the propertyops-healthmonitor systemd unit from this repo.
# Safe to re-run — idempotent: copies, reloads, restarts.

set -euo pipefail

REPO_UNIT="/opt/jade-infra/systemd/propertyops-healthmonitor.service"
LIVE_UNIT="/etc/systemd/system/propertyops-healthmonitor.service"

if [ ! -f "$REPO_UNIT" ]; then
  echo "error: $REPO_UNIT not found" >&2
  exit 1
fi

# Snapshot the live unit (once; never overwrite an existing .bak)
if [ -f "$LIVE_UNIT" ] && [ ! -f "${LIVE_UNIT}.bak" ]; then
  cp -p "$LIVE_UNIT" "${LIVE_UNIT}.bak"
  echo "snapshot: ${LIVE_UNIT}.bak"
fi

cp -p "$REPO_UNIT" "$LIVE_UNIT"
echo "installed: $LIVE_UNIT"

systemctl daemon-reload
systemctl restart propertyops-healthmonitor
sleep 3
systemctl status propertyops-healthmonitor --no-pager
