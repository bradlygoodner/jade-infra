#!/usr/bin/env bash
set -euo pipefail

# ── Load config ──────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/config.env"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# ── Webhook helper ───────────────────────────────────────────────────────────
notify() {
    local service="$1" event="$2" details="${3:-}"
    curl -sf -X POST "${N8N_WEBHOOK_BASE}/upgrade-status" \
        -H "Content-Type: application/json" \
        -d "{
            \"service_name\": \"${service}\",
            \"event\": \"${event}\",
            \"details\": \"${details}\",
            \"timestamp\": \"$(date -Iseconds)\"
        }" 2>/dev/null || log "WARNING: Could not notify n8n (event=${event})"
}

# ── Get current local image digest ───────────────────────────────────────────
get_local_digest() {
    local container="$1"
    local digest
    digest="$(docker inspect --format='{{index .RepoDigests 0}}' \
        "$(docker inspect --format='{{.Image}}' "${container}" 2>/dev/null)" 2>/dev/null \
        | sed 's/.*@//' | tr -d '[:space:]')"
    echo "${digest:-unknown}"
}

# ── Get remote image digest from registry ────────────────────────────────────
get_remote_digest() {
    local image="$1"
    local digest
    digest="$(docker manifest inspect "${image}" 2>/dev/null \
        | grep -m1 '"digest"' | sed 's/.*"digest": *"//;s/".*//' | tr -d '[:space:]')"
    echo "${digest:-unknown}"
}

# ── Save digest to state file ───────────────────────────────────────────────
save_state() {
    local container="$1" image="$2" digest="$3"
    local tmp
    tmp="$(mktemp)"
    _SS_FILE="${STATE_FILE}" _SS_CONTAINER="${container}" _SS_IMAGE="${image}" \
    _SS_DIGEST="${digest}" _SS_TIME="$(date -Iseconds)" _SS_TMP="${tmp}" \
    python3 -c "
import json, os
state_file = os.environ['_SS_FILE']
data = {}
if os.path.isfile(state_file) and os.path.getsize(state_file) > 0:
    with open(state_file) as f:
        data = json.load(f)
data[os.environ['_SS_CONTAINER']] = {
    'image': os.environ['_SS_IMAGE'],
    'digest': os.environ['_SS_DIGEST'].strip(),
    'upgraded_at': os.environ['_SS_TIME'],
}
with open(os.environ['_SS_TMP'], 'w') as f:
    json.dump(data, f, indent=2)
"
    mv "${tmp}" "${STATE_FILE}"
}

# ── Read previous digest from state file ─────────────────────────────────────
get_saved_digest() {
    local container="$1"
    if [ -s "${STATE_FILE}" ]; then
        _SS_FILE="${STATE_FILE}" _SS_CONTAINER="${container}" \
        python3 -c "
import json, os
with open(os.environ['_SS_FILE']) as f:
    data = json.load(f)
print(data.get(os.environ['_SS_CONTAINER'], {}).get('digest', ''))
" 2>/dev/null
    fi
}

# ── Wait for healthcheck to pass ─────────────────────────────────────────────
wait_for_healthy() {
    local container="$1"
    local timeout="${HEALTH_TIMEOUT}"
    local elapsed=0

    log "Waiting for ${container} to become healthy (timeout=${timeout}s)..."
    while [ $elapsed -lt "$timeout" ]; do
        local health
        health="$(docker inspect --format='{{.State.Health.Status}}' "${container}" 2>/dev/null || echo "none")"
        case "${health}" in
            healthy)
                log "${container} is healthy after ${elapsed}s"
                return 0
                ;;
            none)
                # No healthcheck defined — fall back to running state
                local state
                state="$(docker inspect --format='{{.State.Status}}' "${container}" 2>/dev/null || echo "unknown")"
                if [ "${state}" = "running" ]; then
                    log "${container} is running (no healthcheck defined), waiting 10s for startup..."
                    sleep 10
                    return 0
                fi
                ;;
        esac
        sleep "${HEALTH_INTERVAL}"
        elapsed=$((elapsed + HEALTH_INTERVAL))
    done

    log "ERROR: ${container} did not become healthy within ${timeout}s"
    return 1
}

# ── HTTP liveness check (for services without Docker healthcheck) ────────────
wait_for_http() {
    local container="$1" port="$2" path="${3:-/}"
    local timeout="${HEALTH_TIMEOUT}"
    local elapsed=0

    log "Waiting for ${container} HTTP liveness on port ${port}${path} (timeout=${timeout}s)..."
    while [ $elapsed -lt "$timeout" ]; do
        if curl -sf -o /dev/null "http://localhost:${port}${path}" 2>/dev/null; then
            log "${container} is responding on port ${port} after ${elapsed}s"
            return 0
        fi
        sleep "${HEALTH_INTERVAL}"
        elapsed=$((elapsed + HEALTH_INTERVAL))
    done

    log "ERROR: ${container} not responding on port ${port} within ${timeout}s"
    return 1
}

# ── Rollback a single service ────────────────────────────────────────────────
rollback_service() {
    local container="$1" image="$2" previous_digest="$3" compose_file="$4" service_name="$5"

    log "ROLLING BACK ${container} to digest ${previous_digest}..."
    docker pull "${image}@${previous_digest}"
    docker tag "${image}@${previous_digest}" "${image}"
    docker compose -f "${compose_file}" up -d "${service_name}"
}

# ── Upgrade a single service ─────────────────────────────────────────────────
# Returns: 0 = success or skipped, 1 = rollback occurred, 2 = critical failure
upgrade_service() {
    local container="$1"
    local image="$2"
    local compose_file="$3"
    local service_name="$4"
    local health_mode="$5"      # "docker" or "http:<port>[:<path>]"

    log "--- Checking ${container} (${image}) ---"

    # Check if update is available
    local local_digest remote_digest
    local_digest="$(get_local_digest "${container}")"
    remote_digest="$(get_remote_digest "${image}")"

    if [ "${local_digest}" = "${remote_digest}" ] || [ "${remote_digest}" = "unknown" ]; then
        log "${container} is already current (or could not check remote). Skipping."
        return 0
    fi

    log "Update available for ${container}: ${local_digest:0:16}... → ${remote_digest:0:16}..."

    # Save current digest before upgrading
    save_state "${container}" "${image}" "${local_digest}"

    # Notify n8n
    notify "${container}" "starting" "Upgrading from ${local_digest:0:16} to ${remote_digest:0:16}"

    # Pull new image
    log "Pulling new image for ${service_name}..."
    if ! docker compose -f "${compose_file}" pull "${service_name}"; then
        log "ERROR: Failed to pull ${image}"
        notify "${container}" "rollback" "Image pull failed"
        return 1
    fi

    # Restart with new image
    log "Restarting ${service_name}..."
    docker compose -f "${compose_file}" up -d "${service_name}"

    # Wait for health
    local health_ok=0
    case "${health_mode}" in
        docker)
            wait_for_healthy "${container}" && health_ok=1
            ;;
        http:*)
            local port path
            port="$(echo "${health_mode}" | cut -d: -f2)"
            path="$(echo "${health_mode}" | cut -d: -f3-)"
            [ -z "${path}" ] && path="/"
            wait_for_http "${container}" "${port}" "${path}" && health_ok=1
            ;;
    esac

    if [ "${health_ok}" -eq 1 ]; then
        log "SUCCESS: ${container} upgraded successfully"
        notify "${container}" "success" "Upgraded to ${remote_digest:0:16}"
        # Update state with new digest
        save_state "${container}" "${image}" "${remote_digest}"
        return 0
    fi

    # ── Rollback ─────────────────────────────────────────────────────────
    notify "${container}" "rollback" "Healthcheck failed after upgrade, rolling back"
    rollback_service "${container}" "${image}" "${local_digest}" "${compose_file}" "${service_name}"

    # Verify rollback health
    local rollback_ok=0
    case "${health_mode}" in
        docker)
            wait_for_healthy "${container}" && rollback_ok=1
            ;;
        http:*)
            local port path
            port="$(echo "${health_mode}" | cut -d: -f2)"
            path="$(echo "${health_mode}" | cut -d: -f3-)"
            [ -z "${path}" ] && path="/"
            wait_for_http "${container}" "${port}" "${path}" && rollback_ok=1
            ;;
    esac

    if [ "${rollback_ok}" -eq 1 ]; then
        log "Rollback successful for ${container}"
        return 1
    fi

    # Critical: rollback also failed
    log "CRITICAL: Rollback failed for ${container}"
    notify "${container}" "critical" "Both upgrade and rollback failed — manual intervention required"
    return 2
}

# ── Main ─────────────────────────────────────────────────────────────────────
log "========================================="
log "=== Upgrade run started ==="
log "========================================="

# Step 1: Run backups first
log "Running pre-upgrade backup..."
if ! "${SCRIPT_DIR}/backup.sh"; then
    log "CRITICAL: Backup failed — aborting all upgrades"
    notify "system" "critical" "Pre-upgrade backup failed, upgrades aborted"
    exit 1
fi
log "Backup complete."

# Step 2: Upgrade services in tiered order
# Tier 1: DocuSeal (lowest risk, no dependencies)
upgrade_service \
    "propertyops-docuseal" \
    "docuseal/docuseal:latest" \
    "${COMPOSE_BASE}/docuseal/docker-compose.yml" \
    "docuseal" \
    "docker"
result=$?

if [ $result -eq 2 ]; then
    log "Critical failure on DocuSeal. Stopping."
    exit 1
elif [ $result -eq 1 ]; then
    log "DocuSeal rolled back. Stopping remaining upgrades."
    exit 1
fi

log "Soaking for ${SOAK_PERIOD}s before next service..."
sleep "${SOAK_PERIOD}"

# Tier 2: n8n (standalone, needed for notifications)
upgrade_service \
    "propertyops-n8n" \
    "n8nio/n8n:latest" \
    "${COMPOSE_BASE}/n8n/docker-compose.yml" \
    "n8n" \
    "docker"
result=$?

if [ $result -eq 2 ]; then
    log "Critical failure on n8n. Stopping."
    exit 1
elif [ $result -eq 1 ]; then
    log "n8n rolled back. Stopping remaining upgrades."
    exit 1
fi

log "Soaking for ${SOAK_PERIOD}s before next service..."
sleep "${SOAK_PERIOD}"

# Tier 3: Baserow stack — Postgres first, then Redis, then Baserow
upgrade_service \
    "propertyops-postgres" \
    "pgvector/pgvector:pg16" \
    "${COMPOSE_BASE}/baserow/docker-compose.yml" \
    "postgres" \
    "docker"
result=$?

if [ $result -eq 2 ]; then
    log "Critical failure on Postgres. Stopping."
    exit 1
elif [ $result -eq 1 ]; then
    log "Postgres rolled back. Stopping remaining upgrades."
    exit 1
fi

# Short soak between Baserow stack services (60s, not full soak)
sleep 60

upgrade_service \
    "propertyops-redis" \
    "redis:7-alpine" \
    "${COMPOSE_BASE}/baserow/docker-compose.yml" \
    "redis" \
    "docker"
result=$?

if [ $result -eq 2 ]; then
    log "Critical failure on Redis. Stopping."
    exit 1
elif [ $result -eq 1 ]; then
    log "Redis rolled back. Stopping remaining upgrades."
    exit 1
fi

sleep 60

upgrade_service \
    "propertyops-baserow" \
    "baserow/baserow:latest" \
    "${COMPOSE_BASE}/baserow/docker-compose.yml" \
    "baserow" \
    "docker"
result=$?

if [ $result -eq 2 ]; then
    log "Critical failure on Baserow. Stopping."
    exit 1
elif [ $result -eq 1 ]; then
    log "Baserow rolled back. Stopping remaining upgrades."
    exit 1
fi

log "========================================="
log "=== Upgrade run complete ==="
log "========================================="
