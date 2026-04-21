#!/usr/bin/env bash
set -euo pipefail

# ── Load config ──────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/config.env"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# ── Usage ────────────────────────────────────────────────────────────────────
if [ $# -lt 1 ]; then
    echo "Usage: $0 <container_name> [--restore-db]"
    echo ""
    echo "Examples:"
    echo "  $0 propertyops-n8n"
    echo "  $0 propertyops-baserow --restore-db"
    echo ""
    echo "Available services in state file:"
    if [ -s "${STATE_FILE}" ]; then
        _SS_FILE="${STATE_FILE}" python3 -c "
import json, os
with open(os.environ['_SS_FILE']) as f:
    data = json.load(f)
for k, v in data.items():
    print(f\"  {k} → {v['digest'][:16]}... (upgraded {v['upgraded_at']})\")
"
    else
        echo "  (none — no upgrades have been recorded yet)"
    fi
    exit 1
fi

CONTAINER="$1"
RESTORE_DB="${2:-}"

# ── Read previous state ──────────────────────────────────────────────────────
if [ ! -s "${STATE_FILE}" ]; then
    log "ERROR: State file is empty or missing. No rollback data available."
    exit 1
fi

IMAGE="$(_SS_FILE="${STATE_FILE}" _SS_CONTAINER="${CONTAINER}" python3 -c "
import json, os
with open(os.environ['_SS_FILE']) as f:
    data = json.load(f)
entry = data.get(os.environ['_SS_CONTAINER'])
if entry:
    print(entry['image'])
else:
    print('')
")"

DIGEST="$(_SS_FILE="${STATE_FILE}" _SS_CONTAINER="${CONTAINER}" python3 -c "
import json, os
with open(os.environ['_SS_FILE']) as f:
    data = json.load(f)
entry = data.get(os.environ['_SS_CONTAINER'])
if entry:
    print(entry['digest'])
else:
    print('')
")"

if [ -z "${IMAGE}" ] || [ -z "${DIGEST}" ]; then
    log "ERROR: No rollback state found for ${CONTAINER}"
    exit 1
fi

log "Rolling back ${CONTAINER} to ${IMAGE}@${DIGEST:0:16}..."

# ── Determine compose file and service name ──────────────────────────────────
case "${CONTAINER}" in
    propertyops-n8n)
        COMPOSE_FILE="${COMPOSE_BASE}/n8n/docker-compose.yml"
        SERVICE_NAME="n8n"
        ;;
    propertyops-docuseal)
        COMPOSE_FILE="${COMPOSE_BASE}/docuseal/docker-compose.yml"
        SERVICE_NAME="docuseal"
        ;;
    propertyops-baserow)
        COMPOSE_FILE="${COMPOSE_BASE}/baserow/docker-compose.yml"
        SERVICE_NAME="baserow"
        ;;
    propertyops-postgres)
        COMPOSE_FILE="${COMPOSE_BASE}/baserow/docker-compose.yml"
        SERVICE_NAME="postgres"
        ;;
    propertyops-redis)
        COMPOSE_FILE="${COMPOSE_BASE}/baserow/docker-compose.yml"
        SERVICE_NAME="redis"
        ;;
    *)
        log "ERROR: Unknown container ${CONTAINER}"
        exit 1
        ;;
esac

# ── Optional: Restore Postgres backup ────────────────────────────────────────
if [ "${RESTORE_DB}" = "--restore-db" ]; then
    log "Looking for most recent Postgres backup..."
    LATEST_DUMP="$(ls -t "${BACKUP_PATH}/postgres/dump-"*.sql.gz 2>/dev/null | head -1)"
    if [ -z "${LATEST_DUMP}" ]; then
        log "ERROR: No Postgres backup found in ${BACKUP_PATH}/postgres/"
        exit 1
    fi
    log "Restoring Postgres from ${LATEST_DUMP}..."
    log "WARNING: This will overwrite the current database. Proceeding in 5 seconds..."
    sleep 5
    gunzip -c "${LATEST_DUMP}" | docker exec -i propertyops-postgres psql -U "${POSTGRES_USER}" -d "${POSTGRES_USER}" > /dev/null 2>&1
    log "Postgres restore complete."
fi

# ── Pull previous image and restart ──────────────────────────────────────────
log "Pulling ${IMAGE}@${DIGEST}..."
docker pull "${IMAGE}@${DIGEST}"
docker tag "${IMAGE}@${DIGEST}" "${IMAGE}"

log "Restarting ${SERVICE_NAME}..."
docker compose -f "${COMPOSE_FILE}" up -d "${SERVICE_NAME}"

# ── Verify ───────────────────────────────────────────────────────────────────
log "Waiting 30s for service to stabilize..."
sleep 30

STATE="$(docker inspect --format='{{.State.Status}}' "${CONTAINER}" 2>/dev/null || echo "not found")"
if [ "${STATE}" = "running" ]; then
    log "SUCCESS: ${CONTAINER} is running on previous image"
    # Notify n8n
    curl -sf -X POST "${N8N_WEBHOOK_BASE}/upgrade-status" \
        -H "Content-Type: application/json" \
        -d "{
            \"service_name\": \"${CONTAINER}\",
            \"event\": \"rollback\",
            \"details\": \"Manual rollback to ${DIGEST:0:16}\",
            \"timestamp\": \"$(date -Iseconds)\"
        }" 2>/dev/null || log "WARNING: Could not notify n8n"
else
    log "ERROR: ${CONTAINER} is in state '${STATE}' after rollback"
    exit 1
fi
