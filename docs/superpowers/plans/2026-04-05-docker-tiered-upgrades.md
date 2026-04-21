# Docker Tiered Upgrade System — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automated, tiered Docker upgrade system with detection, backup, rollback, and multi-channel notifications for the PropertyOps production stack.

**Architecture:** Watchtower in monitor-only mode detects new images and webhooks n8n. Host shell scripts handle daily backups and weekly upgrades with automatic rollback. n8n workflows provide Pushover alerts, email digests, Google Drive backup sync, and a Baserow upgrade history dashboard.

**Tech Stack:** Docker Compose, Watchtower, Bash, cron, n8n (webhooks + workflows), Pushover, Baserow, Google Drive

**Spec:** `docs/superpowers/specs/2026-04-05-docker-tiered-upgrades-design.md`

---

**Important context for all tasks:**

- Baserow's `DOCKER_DATA_PATH` is `/docker/volumes/propertyops` (set in `docker/baserow/.env`)
- n8n and DocuSeal's `DOCKER_DATA_PATH` is `/root/docker/volumes` (set in their respective `.env` files)
- Postgres credentials: user=`baserow`, db=`baserow` (from `docker/baserow/.env`)
- Redis password is in `docker/baserow/.env`
- n8n runs on port `5678`, DocuSeal on `3001`, Baserow on `8086`
- All compose files are in `docker/<service>/docker-compose.yml`
- The `jade_shared` network already exists and is shared across stacks

---

### Task 1: Create Directory Structure and Config

**Files:**
- Create: `docker/scripts/config.env`
- Create: `docker/backups/image-state.json`

- [ ] **Step 1: Create all required directories**

```bash
mkdir -p /root/docker/scripts
mkdir -p /root/docker/backups/postgres
mkdir -p /root/docker/backups/redis
mkdir -p /root/docker/backups/volumes
mkdir -p /root/docker/logs
```

- [ ] **Step 2: Create the shared config file**

Create `docker/scripts/config.env`:

```bash
#!/usr/bin/env bash
# Shared configuration for backup, upgrade, and rollback scripts
# Sourced by all scripts — not executed directly

# ── Paths ────────────────────────────────────────────────────────────────────
BACKUP_PATH="/root/docker/backups"
LOG_PATH="/root/docker/logs"
STATE_FILE="${BACKUP_PATH}/image-state.json"
COMPOSE_BASE="/root/docker"

# ── Data volume paths (per-service, matching their .env files) ───────────────
# Baserow stack uses a different base path than n8n/DocuSeal
BASEROW_DATA_PATH="/docker/volumes/propertyops"
N8N_DATA_PATH="/root/docker/volumes"
DOCUSEAL_DATA_PATH="/root/docker/volumes"

# ── Service ports (for healthchecks run from host) ───────────────────────────
N8N_PORT=5678
DOCUSEAL_PORT=3001
BASEROW_PORT=8086

# ── Postgres (for pg_dumpall) ────────────────────────────────────────────────
POSTGRES_USER="baserow"

# ── n8n Webhooks ─────────────────────────────────────────────────────────────
N8N_WEBHOOK_BASE="http://localhost:5678/webhook"

# ── Upgrade tuning ───────────────────────────────────────────────────────────
SOAK_PERIOD=300          # seconds between services
HEALTH_TIMEOUT=120       # seconds to wait for healthcheck
HEALTH_INTERVAL=5        # seconds between healthcheck polls
BACKUP_RETENTION=7       # number of backups to keep per target
```

- [ ] **Step 3: Seed an empty image-state.json**

Create `docker/backups/image-state.json`:

```json
{}
```

- [ ] **Step 4: Set permissions**

```bash
chmod 700 /root/docker/backups
chmod 700 /root/docker/backups/postgres
chmod 700 /root/docker/backups/redis
chmod 700 /root/docker/backups/volumes
chmod 600 /root/docker/scripts/config.env
```

- [ ] **Step 5: Verify directory structure**

```bash
ls -la /root/docker/scripts/config.env
ls -la /root/docker/backups/image-state.json
ls -ld /root/docker/backups/postgres /root/docker/backups/redis /root/docker/backups/volumes /root/docker/logs
```

Expected: All files/dirs exist with correct permissions (700 for backup dirs, 600 for config.env).

- [ ] **Step 6: Commit**

```bash
cd /root
git add docker/scripts/config.env docker/backups/image-state.json
git commit -m "feat: add directory structure and shared config for upgrade system"
```

---

### Task 2: Create backup.sh

**Files:**
- Create: `docker/scripts/backup.sh`

- [ ] **Step 1: Write the backup script**

Create `docker/scripts/backup.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

# ── Load config ──────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/config.env"

TIMESTAMP="$(date +%Y-%m-%d-%H%M%S)"
DATE_ONLY="$(date +%Y-%m-%d)"
FAILED=0

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# ── Postgres backup ──────────────────────────────────────────────────────────
backup_postgres() {
    local dest="${BACKUP_PATH}/postgres/dump-${TIMESTAMP}.sql.gz"
    log "Backing up Postgres to ${dest}..."
    if docker exec propertyops-postgres pg_dumpall -U "${POSTGRES_USER}" | gzip > "${dest}"; then
        log "Postgres backup complete: $(du -h "${dest}" | cut -f1)"
    else
        log "ERROR: Postgres backup failed"
        rm -f "${dest}"
        FAILED=1
        return 1
    fi
}

# ── Redis backup ─────────────────────────────────────────────────────────────
backup_redis() {
    local dest="${BACKUP_PATH}/redis/redis-${TIMESTAMP}.rdb"
    log "Backing up Redis to ${dest}..."

    # Read password from baserow .env
    local redis_pass
    redis_pass="$(grep '^REDIS_PASSWORD=' "${COMPOSE_BASE}/baserow/.env" | cut -d= -f2-)"

    # Trigger BGSAVE and wait for completion
    docker exec propertyops-redis redis-cli -a "${redis_pass}" BGSAVE --no-auth-warning > /dev/null 2>&1

    # Wait for BGSAVE to finish (up to 60s)
    local waited=0
    while [ $waited -lt 60 ]; do
        local status
        status="$(docker exec propertyops-redis redis-cli -a "${redis_pass}" LASTSAVE --no-auth-warning 2>/dev/null)"
        sleep 2
        local status2
        status2="$(docker exec propertyops-redis redis-cli -a "${redis_pass}" LASTSAVE --no-auth-warning 2>/dev/null)"
        if [ "$status" = "$status2" ]; then
            break
        fi
        waited=$((waited + 2))
    done

    # Copy the dump file out of the container
    if docker cp propertyops-redis:/data/dump.rdb "${dest}"; then
        log "Redis backup complete: $(du -h "${dest}" | cut -f1)"
    else
        log "ERROR: Redis backup failed"
        rm -f "${dest}"
        FAILED=1
        return 1
    fi
}

# ── Volume backups ───────────────────────────────────────────────────────────
backup_volume() {
    local name="$1"
    local source_path="$2"
    local dest="${BACKUP_PATH}/volumes/${name}-${TIMESTAMP}.tar.gz"
    log "Backing up ${name} volume to ${dest}..."
    if tar czf "${dest}" -C "$(dirname "${source_path}")" "$(basename "${source_path}")"; then
        log "${name} volume backup complete: $(du -h "${dest}" | cut -f1)"
    else
        log "ERROR: ${name} volume backup failed"
        rm -f "${dest}"
        FAILED=1
        return 1
    fi
}

# ── Retention pruning ────────────────────────────────────────────────────────
prune_backups() {
    local dir="$1"
    local pattern="$2"
    local count
    count=$(find "${dir}" -maxdepth 1 -name "${pattern}" -type f | wc -l)
    if [ "${count}" -gt "${BACKUP_RETENTION}" ]; then
        local to_delete=$((count - BACKUP_RETENTION))
        log "Pruning ${to_delete} old backups from ${dir}..."
        find "${dir}" -maxdepth 1 -name "${pattern}" -type f -printf '%T+ %p\n' \
            | sort | head -n "${to_delete}" | awk '{print $2}' \
            | xargs rm -f
    fi
}

# ── Main ─────────────────────────────────────────────────────────────────────
log "=== Backup started ==="

backup_postgres
backup_redis
backup_volume "n8n" "${N8N_DATA_PATH}/n8n"
backup_volume "docuseal" "${DOCUSEAL_DATA_PATH}/docuseal"
backup_volume "baserow" "${BASEROW_DATA_PATH}/baserow"

# Prune old backups
prune_backups "${BACKUP_PATH}/postgres" "dump-*.sql.gz"
prune_backups "${BACKUP_PATH}/redis" "redis-*.rdb"
prune_backups "${BACKUP_PATH}/volumes" "n8n-*.tar.gz"
prune_backups "${BACKUP_PATH}/volumes" "docuseal-*.tar.gz"
prune_backups "${BACKUP_PATH}/volumes" "baserow-*.tar.gz"

# Notify n8n for Google Drive sync (best-effort)
if [ "${FAILED}" -eq 0 ]; then
    log "=== All backups succeeded ==="
    # Fire webhook — don't fail the script if n8n is unreachable
    curl -sf -X POST "${N8N_WEBHOOK_BASE}/backup-complete" \
        -H "Content-Type: application/json" \
        -d "{
            \"timestamp\": \"${TIMESTAMP}\",
            \"date\": \"${DATE_ONLY}\",
            \"files\": {
                \"postgres\": \"${BACKUP_PATH}/postgres/dump-${TIMESTAMP}.sql.gz\",
                \"redis\": \"${BACKUP_PATH}/redis/redis-${TIMESTAMP}.rdb\",
                \"volumes\": [
                    \"${BACKUP_PATH}/volumes/n8n-${TIMESTAMP}.tar.gz\",
                    \"${BACKUP_PATH}/volumes/docuseal-${TIMESTAMP}.tar.gz\",
                    \"${BACKUP_PATH}/volumes/baserow-${TIMESTAMP}.tar.gz\"
                ]
            }
        }" 2>/dev/null || log "WARNING: Could not notify n8n for Google Drive sync"
else
    log "=== Backup completed with errors ==="
    exit 1
fi
```

- [ ] **Step 2: Set permissions**

```bash
chmod 700 /root/docker/scripts/backup.sh
```

- [ ] **Step 3: Test the backup script**

```bash
/root/docker/scripts/backup.sh
```

Expected: All 5 backups created (postgres dump, redis rdb, 3 volume tars). The n8n webhook will fail since the workflow doesn't exist yet — that's expected and non-fatal. Check output for any `ERROR` lines.

- [ ] **Step 4: Verify backup files were created**

```bash
ls -lh /root/docker/backups/postgres/
ls -lh /root/docker/backups/redis/
ls -lh /root/docker/backups/volumes/
```

Expected: One file in each directory with today's timestamp, non-zero sizes.

- [ ] **Step 5: Commit**

```bash
cd /root
git add docker/scripts/backup.sh
git commit -m "feat: add backup script with postgres, redis, and volume backups"
```

---

### Task 3: Create upgrade.sh

**Files:**
- Create: `docker/scripts/upgrade.sh`

- [ ] **Step 1: Write the upgrade script**

Create `docker/scripts/upgrade.sh`:

```bash
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
    docker inspect --format='{{index .RepoDigests 0}}' "$(docker inspect --format='{{.Image}}' "${container}")" 2>/dev/null \
        | sed 's/.*@//' || echo "unknown"
}

# ── Get remote image digest from registry ────────────────────────────────────
get_remote_digest() {
    local image="$1"
    # Use docker manifest inspect to check remote digest without pulling
    docker manifest inspect "${image}" 2>/dev/null \
        | grep -m1 '"digest"' | sed 's/.*"digest": *"//;s/".*//' || echo "unknown"
}

# ── Save digest to state file ───────────────────────────────────────────────
save_state() {
    local container="$1" image="$2" digest="$3"
    local tmp
    tmp="$(mktemp)"
    if [ -s "${STATE_FILE}" ]; then
        # Use python3 for reliable JSON manipulation
        python3 -c "
import json, sys
with open('${STATE_FILE}') as f:
    data = json.load(f)
data['${container}'] = {
    'image': '${image}',
    'digest': '${digest}',
    'upgraded_at': '$(date -Iseconds)'
}
with open('${tmp}', 'w') as f:
    json.dump(data, f, indent=2)
"
    else
        python3 -c "
import json
data = {'${container}': {'image': '${image}', 'digest': '${digest}', 'upgraded_at': '$(date -Iseconds)'}}
with open('${tmp}', 'w') as f:
    json.dump(data, f, indent=2)
"
    fi
    mv "${tmp}" "${STATE_FILE}"
}

# ── Read previous digest from state file ─────────────────────────────────────
get_saved_digest() {
    local container="$1"
    if [ -s "${STATE_FILE}" ]; then
        python3 -c "
import json
with open('${STATE_FILE}') as f:
    data = json.load(f)
print(data.get('${container}', {}).get('digest', ''))
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
    "http:${DOCUSEAL_PORT}:/"
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
    "postgres:16-alpine" \
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
```

- [ ] **Step 2: Set permissions**

```bash
chmod 700 /root/docker/scripts/upgrade.sh
```

- [ ] **Step 3: Dry-run test**

Run the script. Since all services are likely already on their latest pulled image, it should skip all upgrades:

```bash
/root/docker/scripts/upgrade.sh 2>&1 | head -50
```

Expected: Backup runs, then each service shows "already current. Skipping." The script completes without errors.

- [ ] **Step 4: Verify state file was not modified (no upgrades happened)**

```bash
cat /root/docker/backups/image-state.json
```

Expected: Still empty `{}` since no upgrades were performed.

- [ ] **Step 5: Commit**

```bash
cd /root
git add docker/scripts/upgrade.sh
git commit -m "feat: add tiered upgrade script with rollback and healthcheck"
```

---

### Task 4: Create rollback.sh

**Files:**
- Create: `docker/scripts/rollback.sh`

- [ ] **Step 1: Write the rollback script**

Create `docker/scripts/rollback.sh`:

```bash
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
        python3 -c "
import json
with open('${STATE_FILE}') as f:
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

IMAGE="$(python3 -c "
import json
with open('${STATE_FILE}') as f:
    data = json.load(f)
entry = data.get('${CONTAINER}')
if entry:
    print(entry['image'])
else:
    print('')
")"

DIGEST="$(python3 -c "
import json
with open('${STATE_FILE}') as f:
    data = json.load(f)
entry = data.get('${CONTAINER}')
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
```

- [ ] **Step 2: Set permissions**

```bash
chmod 700 /root/docker/scripts/rollback.sh
```

- [ ] **Step 3: Verify usage output**

```bash
/root/docker/scripts/rollback.sh
```

Expected: Shows usage message with "Available services in state file: (none — no upgrades have been recorded yet)".

- [ ] **Step 4: Commit**

```bash
cd /root
git add docker/scripts/rollback.sh
git commit -m "feat: add manual rollback script with optional postgres restore"
```

---

### Task 5: Create Watchtower Compose File

**Files:**
- Create: `docker/watchtower/docker-compose.yml`

- [ ] **Step 1: Write the Watchtower compose file**

Create `docker/watchtower/docker-compose.yml`:

```yaml
services:

  # ── Watchtower (monitor-only) ────────────────────────────────────────────────
  watchtower:
    image: containrrr/watchtower:latest
    container_name: propertyops-watchtower
    restart: unless-stopped
    mem_limit: 256m
    environment:
      WATCHTOWER_MONITOR_ONLY: "true"
      WATCHTOWER_SCHEDULE: "0 0 */6 * * *"
      WATCHTOWER_NOTIFICATIONS: "shoutrrr"
      WATCHTOWER_NOTIFICATION_URL: "generic://propertyops-n8n:5678/webhook/watchtower-update"
      WATCHTOWER_CLEANUP: "false"
      WATCHTOWER_INCLUDE_STOPPED: "false"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks:
      - jade_shared

networks:
  jade_shared:
    external: true
    name: jade_shared
```

- [ ] **Step 2: Start Watchtower**

```bash
docker compose -f /root/docker/watchtower/docker-compose.yml up -d
```

Expected: Watchtower container starts and is running.

- [ ] **Step 3: Verify Watchtower is running and monitor-only**

```bash
docker ps --filter name=propertyops-watchtower --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"
docker logs propertyops-watchtower 2>&1 | tail -5
```

Expected: Container is running. Logs should show something like "Running in monitor only mode" or similar Watchtower startup message.

- [ ] **Step 4: Commit**

```bash
cd /root
git add docker/watchtower/docker-compose.yml
git commit -m "feat: add watchtower in monitor-only mode for update detection"
```

---

### Task 6: Set Up Cron Jobs

**Files:**
- Modify: system crontab

- [ ] **Step 1: Add cron entries**

```bash
(crontab -l 2>/dev/null || true; cat <<'EOF'

# ── PropertyOps: Daily backups + Google Drive sync ───────────────────────────
0 3 * * * /root/docker/scripts/backup.sh >> /root/docker/logs/backup.log 2>&1

# ── PropertyOps: Weekly tiered upgrades (Sunday 2am) ─────────────────────────
0 2 * * 0 /root/docker/scripts/upgrade.sh >> /root/docker/logs/upgrade.log 2>&1
EOF
) | crontab -
```

- [ ] **Step 2: Verify cron entries**

```bash
crontab -l
```

Expected: Both entries are present — daily backup at 3am and weekly upgrade at Sunday 2am.

- [ ] **Step 3: Commit (nothing to commit — cron is system state, but note it in the log)**

No files to commit. Log that cron jobs are configured.

---

### Task 7: Write n8n Workflow Specs

**Files:**
- Create: `docker/n8n/workflows/README-upgrade-workflows.md`

This task creates the detailed n8n workflow specifications that another session (with n8n and Pushover access) will use to build the actual workflows.

- [ ] **Step 1: Create the workflow specs directory**

```bash
mkdir -p /root/docker/n8n/workflows
```

- [ ] **Step 2: Write the workflow specification document**

Create `docker/n8n/workflows/README-upgrade-workflows.md`:

```markdown
# n8n Upgrade System Workflows

Build these 4 workflows in n8n. Each section is a complete workflow spec.

## Prerequisites

- **Pushover credentials** configured in n8n (Settings → Credentials → Pushover API)
- **Email credentials** configured in n8n (SMTP or Gmail)
- **Google Drive credentials** configured in n8n (OAuth2)
- **Baserow API token** — use the internal connection since Baserow is on the same network
  - Base URL: `http://propertyops-baserow/api`

## Baserow Table Setup

Before building workflows, create a table called **Upgrade History** in Baserow with these fields:

| Field Name | Type | Options |
|------------|------|---------|
| service_name | Text | |
| current_digest | Text | |
| available_digest | Text | |
| changelog_url | URL | |
| detected_at | DateTime | Include time |
| upgraded_at | DateTime | Include time |
| status | Single Select | pending, in_progress, completed, rolled_back, critical_failure |
| details | Long Text | |

Note the table ID after creation — it's needed for API calls.

---

## Workflow 1: Update Alert

**Purpose:** When Watchtower detects a new image, fetch the changelog, send a Pushover notification, and log it to Baserow.

### Nodes

1. **Webhook** (trigger)
   - Method: POST
   - Path: `/watchtower-update`
   - Response mode: Immediately
   - This receives the Watchtower shoutrrr payload

2. **Parse Watchtower Payload** (Code node)
   - Watchtower's shoutrrr generic webhook sends a text body
   - Parse out the container name and image info from the message
   - Map container names to GitHub repos:
     ```javascript
     const repoMap = {
       'propertyops-n8n': { owner: 'n8n-io', repo: 'n8n' },
       'propertyops-baserow': { owner: 'bram2w', repo: 'baserow' },
       'propertyops-docuseal': { owner: 'docusealco', repo: 'docuseal' },
     };
     // For postgres and redis, use Docker Hub release pages instead
     const dockerHubMap = {
       'propertyops-postgres': 'https://hub.docker.com/_/postgres/tags',
       'propertyops-redis': 'https://hub.docker.com/_/redis/tags',
     };
     ```

3. **HTTP Request** (fetch changelog)
   - URL: `https://api.github.com/repos/{{ owner }}/{{ repo }}/releases/latest`
   - Method: GET
   - Headers: `Accept: application/vnd.github.v3+json`
   - For postgres/redis: skip this node (no GitHub releases), use Docker Hub link

4. **Pushover** (notification)
   - Title: `Update Available: {{ service_name }}`
   - Message: `New version detected.\n\n{{ changelog_body | truncate(200) }}\n\nFull release: {{ changelog_url }}`
   - Priority: Normal (0)
   - Device: (leave blank for all devices)

5. **Baserow — Create Row** (log to Upgrade History)
   - Table: Upgrade History
   - Fields:
     - service_name: `{{ container_name }}`
     - current_digest: `{{ current_digest }}`
     - available_digest: `{{ new_digest }}`
     - changelog_url: `{{ release_url }}`
     - detected_at: `{{ $now }}`
     - status: `pending`

---

## Workflow 2: Weekly Digest

**Purpose:** Every Monday at 9am, email a summary of pending updates.

### Nodes

1. **Cron** (trigger)
   - Expression: `0 9 * * 1`

2. **Baserow — List Rows** (query pending updates)
   - Table: Upgrade History
   - Filter: `status = "pending"`

3. **IF** (check if any pending)
   - Condition: `{{ $json.results.length > 0 }}`
   - True: continue to format email
   - False: stop (no email if nothing pending)

4. **Code** (format email body)
   ```javascript
   const rows = $input.all();
   let table = '<table border="1" cellpadding="8" cellspacing="0">';
   table += '<tr><th>Service</th><th>Detected</th><th>Changelog</th></tr>';

   for (const row of rows) {
     const detected = new Date(row.json.detected_at);
     const age = Math.floor((Date.now() - detected) / (1000 * 60 * 60 * 24));
     table += `<tr>
       <td>${row.json.service_name}</td>
       <td>${age} days ago</td>
       <td><a href="${row.json.changelog_url}">View</a></td>
     </tr>`;
   }
   table += '</table>';

   return [{
     json: {
       subject: `PropertyOps Weekly Upgrade Digest — ${rows.length} pending update(s)`,
       body: `<h2>Pending Updates</h2>${table}<br><p>Next upgrade window: Sunday 2:00 AM CT</p>`
     }
   }];
   ```

5. **Send Email**
   - To: (your email address)
   - Subject: `{{ $json.subject }}`
   - HTML Body: `{{ $json.body }}`

---

## Workflow 3: Backup Offsite Sync

**Purpose:** Upload backup files to Google Drive when the host script completes a backup.

### Nodes

1. **Webhook** (trigger)
   - Method: POST
   - Path: `/backup-complete`
   - Response mode: Last node

2. **Code** (build file list)
   ```javascript
   const data = $input.first().json;
   const files = [
     { path: data.files.postgres, name: `postgres-dump-${data.timestamp}.sql.gz` },
     { path: data.files.redis, name: `redis-${data.timestamp}.rdb` },
   ];
   for (const vol of data.files.volumes) {
     const basename = vol.split('/').pop();
     files.push({ path: vol, name: basename });
   }
   return files.map(f => ({ json: { ...f, date: data.date } }));
   ```

3. **Read Binary File** (loop over each file)
   - File Path: `{{ $json.path }}`
   - Property Name: `data`

4. **Google Drive — Upload File**
   - File name: `{{ $json.name }}`
   - Parent folder: Create or find folder `PropertyOps Backups/{{ $json.date }}`
   - Binary property: `data`

5. **Respond to Webhook** (on success path)
   - Response body: `{"status": "ok"}`

6. **Error handling** (on error path)
   - **Pushover** notification:
     - Title: `Backup Sync Failed`
     - Message: `{{ $error.message }}`
     - Priority: High (1)
   - **Respond to Webhook**:
     - Response body: `{"status": "failed", "error": "{{ $error.message }}"}`

**Note on file access:** n8n runs inside a container. For it to read backup files from the host, you need to mount the backup directory into the n8n container. Add this volume to `docker/n8n/docker-compose.yml`:

```yaml
volumes:
  - ${DOCKER_DATA_PATH}/n8n:/home/node/.n8n
  - /root/docker/backups:/backups:ro    # ADD THIS LINE
```

Then update the Code node paths to reference `/backups/` instead of `/root/docker/backups/`.

---

## Workflow 4: Upgrade Status Logger

**Purpose:** Log upgrade events to Baserow and send Pushover notifications.

### Nodes

1. **Webhook** (trigger)
   - Method: POST
   - Path: `/upgrade-status`
   - Response mode: Immediately

2. **Switch** (route by event type)
   - Field: `{{ $json.event }}`
   - Cases: `starting`, `success`, `rollback`, `critical`

3a. **On "starting":**
   - **Baserow — List Rows** (find the pending row for this service)
     - Filter: `service_name = {{ $json.service_name }} AND status = "pending"`
     - Take first result
   - **IF** row exists:
     - True → **Baserow — Update Row**: set `status = "in_progress"`
     - False → skip (service may have been upgraded without a Watchtower detection)
   - **Pushover**: Title: `Upgrading {{ $json.service_name }}...`, Priority: Normal (0)

3b. **On "success":**
   - **Baserow — List Rows** (find the in_progress row)
     - Filter: `service_name = {{ $json.service_name }} AND status = "in_progress"`
   - **Baserow — Update Row**: set `status = "completed"`, `upgraded_at = {{ $json.timestamp }}`
   - **Pushover**: Title: `{{ $json.service_name }} upgraded successfully`, Priority: Normal (0)

3c. **On "rollback":**
   - **Baserow — List Rows** (find the in_progress row)
     - Filter: `service_name = {{ $json.service_name }} AND status = "in_progress"`
   - **Baserow — Update Row**: set `status = "rolled_back"`, `details = {{ $json.details }}`
   - **Pushover**: Title: `{{ $json.service_name }} upgrade FAILED — rolled back`, Message: `{{ $json.details }}`, Priority: High (1)

3d. **On "critical":**
   - **Baserow — List Rows**
     - Filter: `service_name = {{ $json.service_name }} AND status IN ("in_progress", "pending")`
   - **Baserow — Update Row**: set `status = "critical_failure"`, `details = {{ $json.details }}`
   - **Pushover**: Title: `CRITICAL: {{ $json.service_name }} rollback FAILED`, Message: `{{ $json.details }}\n\nManual intervention required.`, Priority: Emergency (2), Retry: 60, Expire: 3600

---

## Testing

After building all workflows, test them with these curl commands from the host:

**Test Workflow 1 (Update Alert):**
```bash
curl -X POST http://localhost:5678/webhook/watchtower-update \
  -H "Content-Type: text/plain" \
  -d "Updates available for propertyops-n8n (n8nio/n8n:latest)"
```

**Test Workflow 3 (Backup Sync):**
```bash
curl -X POST http://localhost:5678/webhook/backup-complete \
  -H "Content-Type: application/json" \
  -d '{
    "timestamp": "2026-04-05-030000",
    "date": "2026-04-05",
    "files": {
      "postgres": "/backups/postgres/dump-2026-04-05-030000.sql.gz",
      "redis": "/backups/redis/redis-2026-04-05-030000.rdb",
      "volumes": [
        "/backups/volumes/n8n-2026-04-05-030000.tar.gz",
        "/backups/volumes/docuseal-2026-04-05-030000.tar.gz",
        "/backups/volumes/baserow-2026-04-05-030000.tar.gz"
      ]
    }
  }'
```

**Test Workflow 4 (Upgrade Status):**
```bash
# Test each event type
curl -X POST http://localhost:5678/webhook/upgrade-status \
  -H "Content-Type: application/json" \
  -d '{"service_name": "propertyops-n8n", "event": "starting", "details": "test", "timestamp": "2026-04-05T02:00:00-05:00"}'

curl -X POST http://localhost:5678/webhook/upgrade-status \
  -H "Content-Type: application/json" \
  -d '{"service_name": "propertyops-n8n", "event": "success", "details": "test", "timestamp": "2026-04-05T02:01:00-05:00"}'
```
```

- [ ] **Step 3: Commit**

```bash
cd /root
git add docker/n8n/workflows/README-upgrade-workflows.md
git commit -m "docs: add n8n workflow specs for upgrade system notifications"
```

---

### Task 8: Add Backup Volume Mount to n8n

**Files:**
- Modify: `docker/n8n/docker-compose.yml`

- [ ] **Step 1: Add the backup volume mount**

In `docker/n8n/docker-compose.yml`, add a read-only mount for the backups directory so n8n's Google Drive sync workflow can access backup files:

```yaml
    volumes:
      - ${DOCKER_DATA_PATH}/n8n:/home/node/.n8n
      - /root/docker/backups:/backups:ro
```

- [ ] **Step 2: Restart n8n to pick up the new volume**

```bash
docker compose -f /root/docker/n8n/docker-compose.yml up -d
```

- [ ] **Step 3: Verify the mount works**

```bash
docker exec propertyops-n8n ls /backups/
```

Expected: Shows `postgres`, `redis`, `volumes`, and `image-state.json`.

- [ ] **Step 4: Commit**

```bash
cd /root
git add docker/n8n/docker-compose.yml
git commit -m "feat: mount backup directory into n8n for google drive sync workflow"
```

---

### Task 9: Add DocuSeal Healthcheck

**Files:**
- Modify: `docker/docuseal/docker-compose.yml`

The upgrade script uses Docker healthchecks when available. DocuSeal doesn't have one — add it for consistency with the other services.

- [ ] **Step 1: Add healthcheck to DocuSeal compose**

Add a healthcheck block to the `docuseal` service in `docker/docuseal/docker-compose.yml`:

```yaml
    healthcheck:
      test: ["CMD-SHELL", "curl -fs http://localhost:3000/ || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 30s
```

- [ ] **Step 2: Update the upgrade script to use Docker healthcheck for DocuSeal**

In `docker/scripts/upgrade.sh`, change the DocuSeal `upgrade_service` call from `http:${DOCUSEAL_PORT}:/` to `docker`:

```bash
upgrade_service \
    "propertyops-docuseal" \
    "docuseal/docuseal:latest" \
    "${COMPOSE_BASE}/docuseal/docker-compose.yml" \
    "docuseal" \
    "docker"
```

- [ ] **Step 3: Restart DocuSeal to apply the healthcheck**

```bash
docker compose -f /root/docker/docuseal/docker-compose.yml up -d
```

- [ ] **Step 4: Verify healthcheck is active**

```bash
# Wait 30s for start_period, then check
sleep 35
docker inspect --format='{{.State.Health.Status}}' propertyops-docuseal
```

Expected: `healthy`

- [ ] **Step 5: Commit**

```bash
cd /root
git add docker/docuseal/docker-compose.yml docker/scripts/upgrade.sh
git commit -m "feat: add healthcheck to docuseal, use docker healthcheck in upgrade script"
```

---

### Task 10: Final Verification

- [ ] **Step 1: Verify all containers are running**

```bash
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"
```

Expected: All 6 containers running (5 original + watchtower). All healthcheck-enabled services show "healthy".

- [ ] **Step 2: Verify all scripts are executable**

```bash
ls -la /root/docker/scripts/
```

Expected: `backup.sh`, `upgrade.sh`, `rollback.sh` all have `-rwx------` permissions. `config.env` has `-rw-------`.

- [ ] **Step 3: Verify cron is configured**

```bash
crontab -l | grep -E "(backup|upgrade)"
```

Expected: Two cron entries — daily backup at 3am, weekly upgrade at Sunday 2am.

- [ ] **Step 4: Run a full backup to confirm everything works end-to-end**

```bash
/root/docker/scripts/backup.sh 2>&1
```

Expected: All backups succeed. Google Drive webhook warning is expected (workflow not built yet).

- [ ] **Step 5: Verify backup files**

```bash
echo "=== Postgres ===" && ls -lh /root/docker/backups/postgres/
echo "=== Redis ===" && ls -lh /root/docker/backups/redis/
echo "=== Volumes ===" && ls -lh /root/docker/backups/volumes/
```

Expected: Backup files exist with non-zero sizes.
