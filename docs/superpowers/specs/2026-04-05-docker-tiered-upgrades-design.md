# Docker Tiered Upgrade System — Design Spec

## Overview

Automated, tiered Docker container upgrade system for the PropertyOps production stack. Separates update detection from execution, with mandatory pre-upgrade backups, automatic rollback on failure, and multi-channel notifications.

## Current Stack

| Service | Image | Compose File | Dependencies |
|---------|-------|-------------|-------------|
| Baserow | `baserow/baserow:latest` | `docker/baserow/docker-compose.yml` | Postgres, Redis |
| Postgres | `postgres:16-alpine` | `docker/baserow/docker-compose.yml` | None |
| Redis | `redis:7-alpine` | `docker/baserow/docker-compose.yml` | None |
| n8n | `n8nio/n8n:latest` | `docker/n8n/docker-compose.yml` | None |
| DocuSeal | `docuseal/docuseal:latest` | `docker/docuseal/docker-compose.yml` | None |

All application services use `:latest` tags. Services share the `jade_shared` network. Postgres and Redis are on an internal bridge network scoped to the Baserow stack.

## Architecture

Three layers, each with a single responsibility:

```
Watchtower (monitor-only)
    |
    | webhook: "new image available"
    v
n8n (notification + visibility)
    |
    | Pushover alerts, email digests, Baserow logging
    v
Host scripts (backup + upgrade + rollback)
    |
    | webhook: "upgrade starting/succeeded/failed"
    v
n8n (logs result, sends alerts)
```

---

## Layer 1: Detection — Watchtower Monitor-Only

### Purpose

Poll Docker Hub for newer images. Never pull or restart — only detect and notify.

### Configuration

New compose file: `docker/watchtower/docker-compose.yml`

```yaml
services:
  watchtower:
    image: containrrr/watchtower:latest
    container_name: propertyops-watchtower
    restart: unless-stopped
    mem_limit: 256m
    environment:
      WATCHTOWER_MONITOR_ONLY: "true"
      WATCHTOWER_SCHEDULE: "0 0 */6 * * *"  # Every 6 hours
      WATCHTOWER_NOTIFICATION_URL: "generic://propertyops-n8n:5678/webhook/watchtower-update"
      WATCHTOWER_NOTIFICATIONS: "shoutrrr"
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

### Behavior

- Checks all running containers every 6 hours for newer images on Docker Hub.
- On detection, sends a webhook payload to n8n containing: container name, current image digest, new image digest.
- Read-only Docker socket access — cannot modify containers.

---

## Layer 2: Notification & Visibility — n8n Workflows

### Workflow 1: Update Alert

**Trigger:** Webhook from Watchtower (`/webhook/watchtower-update`)

**Steps:**
1. Parse Watchtower payload — extract service name, current digest, new digest.
2. Map service name to its GitHub releases URL:
   - `n8nio/n8n` → `https://github.com/n8n-io/n8n/releases`
   - `baserow/baserow` → `https://github.com/bram2w/baserow/releases`
   - `docuseal/docuseal` → `https://github.com/docusealco/docuseal/releases`
3. Fetch latest release notes from GitHub API (`GET /repos/{owner}/{repo}/releases/latest`).
4. Send Pushover notification:
   - Title: "Update Available: [service name]"
   - Body: Version info + first 200 chars of changelog + link to full release notes
   - Priority: normal (0)
5. Log to Baserow "Upgrade History" table:
   - Fields: `service_name`, `current_digest`, `available_digest`, `changelog_url`, `detected_at`, `status` (set to "pending")

### Workflow 2: Weekly Digest

**Trigger:** Cron — every Monday at 9:00 AM (`0 9 * * 1`)

**Steps:**
1. Query Baserow "Upgrade History" table for rows where `status = "pending"`.
2. If no pending updates, skip — no email sent.
3. Format email with table of pending updates: service name, how long ago detected, changelog link.
4. Send summary email to configured recipient(s).
5. Subject: "PropertyOps Weekly Upgrade Digest — [count] pending updates"

### Workflow 3: Backup Offsite Sync

**Trigger:** Webhook from host script (`/webhook/backup-complete`)

**Steps:**
1. Receive payload with backup file paths and timestamp.
2. Upload each backup file to Google Drive folder: `PropertyOps Backups/YYYY-MM-DD/`.
3. On success: respond to webhook with `{"status": "ok"}`.
4. On failure: respond with `{"status": "failed", "error": "..."}` and send Pushover notification:
   - Title: "Backup Sync Failed"
   - Body: Error details
   - Priority: high (1)

### Workflow 4: Upgrade Status Logger

**Trigger:** Webhook from host script (`/webhook/upgrade-status`)

**Steps:**
1. Parse payload: `service_name`, `event` (starting | success | rollback | critical), `details`, `timestamp`.
2. Update Baserow "Upgrade History" table:
   - On "starting": update status to "in_progress"
   - On "success": update status to "completed", set `upgraded_at` timestamp
   - On "rollback": update status to "rolled_back", set `details`
   - On "critical": update status to "critical_failure", set `details`
3. Send Pushover notification based on event type:
   - "starting": Priority normal (0) — "Upgrading [service]..."
   - "success": Priority normal (0) — "[service] upgraded successfully"
   - "rollback": Priority high (1) — "[service] upgrade failed — rolled back to previous version"
   - "critical": Priority emergency (2) — "[service] rollback failed — manual intervention required"

### Baserow Table: Upgrade History

| Field | Type | Description |
|-------|------|-------------|
| `service_name` | Text | Container name (e.g., `propertyops-n8n`) |
| `current_digest` | Text | Image digest before upgrade |
| `available_digest` | Text | New image digest detected |
| `changelog_url` | URL | Link to GitHub release notes |
| `detected_at` | DateTime | When Watchtower detected the update |
| `upgraded_at` | DateTime | When upgrade was applied (null if pending) |
| `status` | Single Select | pending, in_progress, completed, rolled_back, critical_failure |
| `details` | Long Text | Error messages, rollback info |

---

## Layer 3: Host Scripts — Backup, Upgrade, Rollback

### File: `docker/scripts/backup.sh`

**Purpose:** Create local backups of all data. Run daily and as part of the upgrade pipeline.

**Backup targets:**

1. **Postgres** — `docker exec propertyops-postgres pg_dumpall -U $POSTGRES_USER | gzip > /root/docker/backups/postgres/dump-YYYY-MM-DD-HHMMSS.sql.gz`
2. **Redis** — `docker exec propertyops-redis redis-cli -a $REDIS_PASSWORD BGSAVE`, wait for completion, then `cp` the RDB file to `/root/docker/backups/redis/redis-YYYY-MM-DD-HHMMSS.rdb`
3. **Application volumes** — `tar czf /root/docker/backups/volumes/[service]-YYYY-MM-DD-HHMMSS.tar.gz [volume_path]` for each of:
   - n8n: `$DOCKER_DATA_PATH/n8n`
   - DocuSeal: `$DOCKER_DATA_PATH/docuseal`
   - Baserow: `$DOCKER_DATA_PATH/baserow`

**Retention:** Keep last 7 backups per target. Prune older files after each run.

**Post-backup:** Fire webhook to n8n (`/webhook/backup-complete`) with list of backup file paths for Google Drive sync. If the webhook or Google Drive upload fails, log a warning but do not fail the backup — local backups are the rollback path.

**Exit codes:** Exit 0 on success, exit 1 on any backup failure (used by upgrade script to gate upgrades).

### File: `docker/scripts/upgrade.sh`

**Purpose:** Orchestrate tiered upgrades with backup, healthcheck, and rollback.

**Rollout order:**
1. DocuSeal (lowest risk, no dependencies)
2. n8n (standalone, needed for notifications of subsequent upgrades)
3. Baserow stack: Postgres → Redis → Baserow (respects dependency chain)

**Configuration (top of script):**
- `SOAK_PERIOD=300` — seconds to wait between services (default 5 minutes)
- `HEALTH_TIMEOUT=120` — seconds to wait for healthcheck after restart
- `HEALTH_INTERVAL=5` — seconds between healthcheck polls
- `STATE_FILE="/root/docker/backups/image-state.json"`
- `N8N_WEBHOOK_BASE="http://localhost:5678/webhook"`

**Per-service upgrade flow:**

```
1. Compare current image digest with remote digest
   └─ If same → skip, log "already current"
   └─ If different → continue

2. Run backup.sh
   └─ If exit 1 → abort all upgrades, send critical alert

3. POST to n8n: {"service": "...", "event": "starting"}

4. Save current digest to STATE_FILE

5. docker compose -f [compose_file] pull [service]

6. docker compose -f [compose_file] up -d [service]

7. Poll healthcheck for HEALTH_TIMEOUT seconds
   └─ If healthy → POST to n8n: {"event": "success"}
   └─ If unhealthy → ROLLBACK:
       a. docker pull [image]@[previous_digest]
       b. docker tag [image]@[previous_digest] [image]:latest
       c. docker compose -f [compose_file] up -d [service]
       d. Verify healthcheck passes after rollback
          └─ If healthy → POST to n8n: {"event": "rollback"}
          └─ If unhealthy → POST to n8n: {"event": "critical"}, EXIT

8. Sleep SOAK_PERIOD before next service
```

**On any rollback:** Stop processing remaining services. Do not upgrade downstream services when an upstream one failed.

**For DocuSeal** (no native healthcheck in compose): The script checks HTTP 200 on `http://localhost:${DOCUSEAL_PORT}/` as a basic liveness check.

### File: `docker/scripts/rollback.sh`

**Purpose:** Manual rollback of a single service to its previous image.

**Usage:** `./rollback.sh <service_name>` (e.g., `./rollback.sh propertyops-n8n`)

**Steps:**
1. Read previous digest from `image-state.json` for the given service.
2. If no previous state exists, exit with error.
3. Pull the previous image by digest.
4. Retag and restart the service.
5. For Baserow: prompt whether to also restore Postgres backup (since a Baserow upgrade may include DB migrations that need reverting).
6. Verify healthcheck.
7. POST to n8n with rollback event.

### File: `docker/backups/image-state.json`

Tracks the last known-good image digest per service:

```json
{
  "propertyops-docuseal": {
    "image": "docuseal/docuseal:latest",
    "digest": "sha256:...",
    "upgraded_at": "2026-04-05T02:00:00Z"
  },
  "propertyops-n8n": {
    "image": "n8nio/n8n:latest",
    "digest": "sha256:...",
    "upgraded_at": "2026-04-05T02:05:00Z"
  },
  "propertyops-baserow": {
    "image": "baserow/baserow:latest",
    "digest": "sha256:...",
    "upgraded_at": "2026-04-05T02:12:00Z"
  },
  "propertyops-postgres": {
    "image": "postgres:16-alpine",
    "digest": "sha256:...",
    "upgraded_at": "2026-04-05T02:10:00Z"
  },
  "propertyops-redis": {
    "image": "redis:7-alpine",
    "digest": "sha256:...",
    "upgraded_at": "2026-04-05T02:11:00Z"
  }
}
```

---

## Cron Schedule

```cron
# Daily backups + Google Drive sync — 3:00 AM
0 3 * * * /root/docker/scripts/backup.sh >> /root/docker/logs/backup.log 2>&1

# Weekly upgrades — Sunday 2:00 AM
0 2 * * 0 /root/docker/scripts/upgrade.sh >> /root/docker/logs/upgrade.log 2>&1
```

Log files at `/root/docker/logs/` with rotation handled by logrotate or a simple size check in the scripts.

---

## Directory Structure

```
docker/
  baserow/
    docker-compose.yml    # existing
    .env                  # existing
  n8n/
    docker-compose.yml    # existing
    .env                  # existing
  docuseal/
    docker-compose.yml    # existing
    .env                  # existing
  watchtower/
    docker-compose.yml    # new
  scripts/
    upgrade.sh            # new
    backup.sh             # new
    rollback.sh           # new
    config.env            # new — shared config (paths, timeouts, webhook URLs)
  backups/
    postgres/             # new
    redis/                # new
    volumes/              # new
    image-state.json      # new
  logs/                   # new
  volumes/                # existing
```

### File: `docker/scripts/config.env`

Shared configuration sourced by all scripts:

```bash
DOCKER_DATA_PATH="/root/docker/volumes"
BACKUP_PATH="/root/docker/backups"
LOG_PATH="/root/docker/logs"
STATE_FILE="${BACKUP_PATH}/image-state.json"
N8N_WEBHOOK_BASE="http://localhost:5678/webhook"
SOAK_PERIOD=300
HEALTH_TIMEOUT=120
HEALTH_INTERVAL=5
BACKUP_RETENTION=7
```

---

## Failure Modes & Responses

| Failure | Response |
|---------|----------|
| Backup fails | Abort all upgrades, Pushover critical alert |
| Google Drive sync fails | Log warning, continue upgrades (local backups exist) |
| Image pull fails | Skip that service, Pushover alert, stop remaining services (likely network issue) |
| Healthcheck fails post-upgrade | Auto-rollback to previous digest, stop remaining services, Pushover high alert |
| Rollback fails | Pushover emergency alert (requires ack), stop everything, manual intervention required |
| n8n is down when webhook fires | Script logs locally, notifications are best-effort (n8n may pick up logs after its own upgrade) |
| Watchtower can't reach Docker Hub | Silent — no webhook fires, next poll retries. Watchtower logs the error. |

---

## Security Considerations

- Docker socket is mounted **read-only** into Watchtower — it cannot modify containers.
- Host scripts run as root (needed for Docker commands). Scripts should be owned by root with `700` permissions.
- Webhook endpoints on n8n should use unique, non-guessable paths. Since n8n and Watchtower communicate over `jade_shared` (not exposed publicly), the webhook is not internet-reachable.
- Backup files may contain sensitive data (Postgres dumps). Backup directories should have `700` permissions.
- `config.env` may contain credentials if extended — should have `600` permissions.
