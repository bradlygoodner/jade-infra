# Service Health Monitor Design

**Date:** 2026-04-07
**Status:** Draft
**Related:** [Docker Tiered Upgrades Design](2026-04-05-docker-tiered-upgrades-design.md)

## Problem

Baserow (and potentially other services) can enter a 500-error state silently. There is no proactive detection, notification, or automated recovery — issues are only discovered manually after the fact.

## Solution Overview

A Python-based health monitor running as a systemd service on the host. It checks all services every 30 seconds, auto-restarts on failure, and sends notifications through a three-tier system. An n8n workflow provides an intelligent operations hub with LLM-assisted diagnosis for persistent failures.

## Services Monitored

| Service | Internal Endpoint | Public Endpoint | Compose File |
|---------|-------------------|-----------------|--------------|
| Baserow | `http://localhost:8086/api/_health/` | `https://app.jadepropertiesgroup.com/api/_health/` | `docker/baserow/docker-compose.yml` |
| n8n | `http://localhost:5678/healthz` | `https://automation.jadepropertiesgroup.com/healthz` | `docker/n8n/docker-compose.yml` |
| DocuSeal | `http://localhost:3001/` | DocuSeal public URL (if tunneled) | `docker/docuseal/docker-compose.yml` |

**Success criteria per check:** HTTP 200 within 10 seconds.

## Failure Classification

Per service, each check cycle evaluates both internal and public endpoints:

- **Internal fails** -> Service issue -> Trigger restart
- **Internal healthy, public fails** -> Tunnel issue -> Alert only (no restart)
- **Both fail** -> Assume service issue -> Trigger restart

## Restart & Recovery Logic

Each service tracks its own failure state independently.

### Detection Threshold

2 consecutive internal failures (60 seconds) triggers a restart.

### Restart Method

- **Baserow:** Check Postgres and Redis health first. If a dependency is down, restart the dependency, wait for healthy, then restart Baserow. First attempt restarts only the Baserow container. If that fails, restart the full stack (Postgres, Redis, Baserow).
- **n8n, DocuSeal:** Straightforward `docker compose restart`.

### Cooldown

5 minutes after each restart before allowing another attempt on the same service.

### Max Restarts

3 per service within a 30-minute rolling window. After 3 failed restarts:

- Stop restart attempts for that service
- Send high-priority emergency Pushover alert
- Continue monitoring other services normally
- Resume restart attempts if the service self-recovers

### Recovery

Once a service returns to healthy, reset failure counter and restart count. Send a recovery notification.

## Three-Tier Notification System

### Tier 1: n8n Webhook (Primary)

- POST to `http://localhost:5678/webhook/health-alert`
- Payload:
  ```json
  {
    "service": "baserow",
    "status": "unhealthy",
    "event_type": "restart_initiated",
    "message": "Baserow failed 2 consecutive health checks, restarting",
    "timestamp": "2026-04-07T03:15:00Z",
    "restart_count": 1,
    "check_type": "internal"
  }
  ```
- `event_type` values: `failure_detected`, `restart_initiated`, `restart_success`, `restart_failed`, `emergency`, `recovery`, `tunnel_issue`
- n8n workflow routes to Pushover with appropriate priority

### Tier 2: Direct Pushover API (Fallback)

Used when n8n webhook fails or n8n itself is the unhealthy service.

- POST to `https://api.pushover.net/1/messages.json`
- Priority mapping:
  - Normal restart -> priority 0 (normal)
  - Tunnel issue -> priority 0 (normal)
  - Emergency (3 failed restarts) -> priority 2 (emergency, requires acknowledgement, retry=60, expire=3600)
  - Recovery -> priority -1 (silent/low)

### Tier 3: Uptime Kuma Heartbeat (Dead Man's Switch)

- Push to Uptime Kuma's push monitor URL every 30 seconds
- If this server goes completely offline, Uptime Kuma detects missing heartbeats and alerts independently
- Catches the scenario where the host itself is unresponsive

### Notification Deduplication

Only notify on state transitions:
- healthy -> unhealthy
- unhealthy -> restarting
- restarting -> emergency
- any state -> recovered

No repeated "still down" alerts.

## Script Architecture

### File: `docker/scripts/healthmonitor.py`

**Dependencies:** Python 3 + `requests` library. No heavy frameworks.

**State management:** In-memory. The script runs as a long-lived systemd process. If it restarts, counters reset cleanly.

**Internal structure:**

- `ServiceConfig` dataclass — name, internal URL, public URL, compose file path, restart command, dependencies
- `ServiceState` — consecutive failures, restart count, last restart time, cooldown status, emergency flag
- `HealthMonitor` class — main loop, per-service check logic, restart logic, dependency-aware restart ordering
- `NotificationManager` — tries n8n webhook, falls back to direct Pushover, sends Uptime Kuma heartbeat every cycle

**Logging:** Python `logging` module to stdout (captured by journalctl) and `/root/docker/logs/healthmonitor.log`.

**Configuration:** Reads from `docker/scripts/config.env` where possible (Pushover credentials, Uptime Kuma URL, n8n webhook base URL). Thresholds have sensible defaults:

- `CHECK_INTERVAL=30` (seconds)
- `FAILURE_THRESHOLD=2` (consecutive failures before restart)
- `COOLDOWN_PERIOD=300` (seconds between restarts)
- `MAX_RESTARTS=3` (per 30-minute window)
- `REQUEST_TIMEOUT=10` (seconds per HTTP check)

### Systemd Unit: `propertyops-healthmonitor.service`

- `Restart=always` with `RestartSec=10` — systemd restarts the monitor if it crashes
- `After=docker.service` — starts after Docker is available
- `WantedBy=multi-user.target` — enabled on boot

## n8n Self-Healing Workflow

### Webhook Trigger

Receives POST at `/webhook/health-alert` from the Python monitor.

### Branch 1: All Events (Logging + Notification)

1. Log event to Baserow "Service Health" table
2. Send Pushover notification via n8n (with formatting and context)

### Branch 2: Emergency Events Only (LLM Diagnosis)

Triggered when `event_type` is `emergency` (3 failed restarts).

1. **Gather context** via Execute Command node:
   - `docker logs --tail 200 <container>`
   - `docker inspect <container>` (state, health, restarts)
   - Disk usage (`df -h`)
   - Memory usage (`free -m`)
2. **Send to Claude API** with diagnostic prompt:
   - "This service has failed 3 restart attempts. Here are the logs and system state. What is likely wrong and what remediation steps should be tried?"
3. **Append LLM diagnosis** to the Baserow row
4. **Send enriched Pushover alert** with diagnosis summary

**Important boundary:** The LLM suggests remediation only. It does not execute commands. The human reviews the diagnosis and acts.

**Claude API credentials:** Stored in n8n's credential manager (not in config.env on the host).

### Baserow "Service Health" Table

| Field | Type | Notes |
|-------|------|-------|
| Timestamp | DateTime | Auto-populated |
| Service | Single Select | Baserow, n8n, DocuSeal |
| Event Type | Single Select | failure, restart, recovery, emergency, tunnel_issue |
| Check Type | Single Select | internal, public |
| Restart Count | Number | Current count in window |
| LLM Diagnosis | Long Text | Populated on emergency events only |
| Resolved | Boolean | Manual toggle |
| Resolution Notes | Long Text | Manual post-incident notes |

## File Layout

```
docker/scripts/
  healthmonitor.py          # Main monitor script
  config.env                # Existing config, extended with new vars

/etc/systemd/system/
  propertyops-healthmonitor.service  # Systemd unit file

docker/logs/
  healthmonitor.log         # Monitor log output
```

## Integration with Existing Infrastructure

- **config.env:** Extended with Pushover API credentials, Uptime Kuma push URL, health monitor thresholds
- **upgrade.sh:** No changes needed. The health monitor will detect if an upgrade causes issues and handle restart/notification.
- **Watchtower:** Unaffected. Continues image update detection independently.
- **Backup:** Unaffected. Runs on its own cron schedule.
