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
        chmod 644 "${dest}"
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
    shift 2
    # Remaining args are extra tar flags (e.g. --exclude patterns)
    local dest="${BACKUP_PATH}/volumes/${name}-${TIMESTAMP}.tar.gz"
    log "Backing up ${name} volume to ${dest}..."
    if tar czf "${dest}" "$@" -C "$(dirname "${source_path}")" "$(basename "${source_path}")"; then
        log "${name} volume backup complete: $(du -h "${dest}" | cut -f1)"
    else
        log "ERROR: ${name} volume backup failed"
        rm -f "${dest}"
        FAILED=1
        return 1
    fi
}

# ── n8n execution binary data cleanup ────────────────────────────────────────
prune_n8n_executions() {
    local storage="${N8N_DATA_PATH}/n8n/storage/workflows"
    if [ ! -d "${storage}" ]; then
        return 0
    fi
    local count
    count=$(find "${storage}" -mindepth 3 -maxdepth 3 -type d -name 'binary_data' -mtime +7 -printf '%h\n' | wc -l)
    if [ "${count}" -gt 0 ]; then
        find "${storage}" -mindepth 2 -maxdepth 2 -type d -name 'executions' -exec \
            find {} -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf {} + \;
        log "Pruned ${count} n8n execution directories older than 7 days"
    fi
}

# ── Docker image cleanup ────────────────────────────────────────────────────
prune_docker_images() {
    local reclaimed
    reclaimed=$(docker image prune -f 2>/dev/null | grep 'Total reclaimed space' || echo "none")
    log "Docker image prune: ${reclaimed}"
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

# ── Postgres maintenance ─────────────────────────────────────────────────
maintain_postgres() {
    log "Running VACUUM ANALYZE on high-churn tables..."
    docker exec propertyops-postgres psql -U "${POSTGRES_USER}" -d baserow -c "
        VACUUM ANALYZE database_field;
        VACUUM ANALYZE database_linkrowfield;
        VACUUM ANALYZE database_table;
        VACUUM ANALYZE core_action;
        VACUUM ANALYZE database_rowhistory;
        VACUUM ANALYZE baserow_enterprise_auditlogentry;
    " > /dev/null 2>&1 && log "Postgres maintenance complete" \
        || log "WARNING: Postgres maintenance had errors (non-fatal)"

    # Trim audit log entries older than 90 days
    local deleted
    deleted=$(docker exec propertyops-postgres psql -U "${POSTGRES_USER}" -d baserow -tAc "
        DELETE FROM baserow_enterprise_auditlogentry
        WHERE action_timestamp < NOW() - INTERVAL '90 days'
        RETURNING 1;" 2>/dev/null | wc -l)
    if [ "${deleted}" -gt 0 ]; then
        log "Trimmed ${deleted} audit log entries older than 90 days"
    fi
}

# ── Media integrity check ────────────────────────────────────────────────
check_media_integrity() {
    log "Checking media file integrity..."

    # Count files tracked in database
    local db_count
    db_count=$(docker exec propertyops-postgres psql -U "${POSTGRES_USER}" -d baserow -tAc \
        "SELECT count(*) FROM core_userfile;" 2>/dev/null)

    # Count actual files on disk
    local disk_count
    disk_count=$(docker exec propertyops-baserow find /baserow/data/media/user_files -type f 2>/dev/null | wc -l)

    # Media directory size
    local media_size
    media_size=$(docker exec propertyops-baserow du -sh /baserow/data/media/ 2>/dev/null | cut -f1)

    log "Media: ${db_count:-?} files in DB, ${disk_count:-?} files on disk, ${media_size:-?} total"

    # Warn if disk has significantly more files than DB (orphaned files)
    if [ -n "${db_count}" ] && [ -n "${disk_count}" ]; then
        local orphan_estimate=$((disk_count - db_count))
        if [ "${orphan_estimate}" -gt 100 ]; then
            log "WARNING: ~${orphan_estimate} potentially orphaned media files detected"
        fi
    fi

    # Check disk space remaining
    local avail_gb
    avail_gb=$(df -BG "${BASEROW_DATA_PATH}/" 2>/dev/null | awk 'NR==2{print $4}' | tr -d 'G')
    if [ -n "${avail_gb}" ] && [ "${avail_gb}" -lt 5 ]; then
        log "CRITICAL: Only ${avail_gb}GB disk space remaining!"
        FAILED=1
    elif [ -n "${avail_gb}" ] && [ "${avail_gb}" -lt 10 ]; then
        log "WARNING: Only ${avail_gb}GB disk space remaining"
    fi
}

# ── Main ─────────────────────────────────────────────────────────────────────
log "=== Backup started ==="

check_media_integrity
backup_postgres
maintain_postgres
backup_redis
backup_volume "n8n" "${N8N_DATA_PATH}/n8n" --exclude='n8n/storage/workflows/*/executions/*'
backup_volume "docuseal" "${DOCUSEAL_DATA_PATH}/docuseal"
backup_volume "baserow" "${BASEROW_DATA_PATH}/baserow"

# Prune stale data
prune_n8n_executions
prune_docker_images
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
