# Baserow FD-count monitoring in healthmonitor.py

**Status:** Design — awaiting user review
**Date:** 2026-04-21
**Owner:** Brad (infra) + Claude
**Bucket:** 2 of 5 from the post-incident optimization plan agreed 2026-04-21

## Background

On 2026-04-20 around 20:30–20:40 local, Coltons Plants Baserow served a burst of HTTP 500s, primarily on `POST /api/user-files/upload-file/` (17 sequential failures of the same SHA256 photo from the plant website's camera flow), with collateral 500s on auth and dashboard endpoints during the same window. Postgres and Redis were healthy throughout. Root cause was `OSError: [Errno 24] Too many open files` inside the baserow container — file-descriptor exhaustion. The container was running with the Linux default soft `nofile` limit of 1024.

The existing `healthmonitor.py` script did not detect or react to the incident because its only signal is `GET /api/_health/`, which kept returning 200 throughout. A separate manual `systemctl restart docker` resolved the immediate user impact by resetting the FD count, but masked the underlying problem.

A bandaid (Bucket 1 — already shipped) raised the container's `nofile` soft/hard limits to 65535 via a `ulimits:` block in `docker/baserow/docker-compose.yml`. That delays the failure mode but does not detect it. This spec is Bucket 2 — proactive FD-count monitoring so the next slow build-up gets caught before users see errors.

## Goal

Add an FD-count probe to `healthmonitor.py` that:
- Samples the baserow container's process-tree FD usage every health-check cycle.
- Fires a warning notification at 70% of the soft limit.
- Fires a critical notification and proactively restarts the baserow container at 90% of the soft limit.
- Reuses existing notification, restart, and cooldown machinery so the new path inherits the same safety guarantees as the existing 5xx-style restarts.
- Stays baserow-specific in v1 (n8n and docuseal do not exhibit this failure mode).

## Non-goals

- Historical FD trending or dashboards — requires a TSDB and is out of scope.
- Auto-tuning the `nofile` limit at runtime — would require compose-level changes and a recreate, not a runtime fix.
- Applying the probe to n8n or docuseal — neither has shown FD pressure; we add it later if needed.
- Diagnosing or fixing the underlying baserow leak — that's an upstream concern; this spec is detection, not remediation of the root code bug.

## Architecture

### Component placement

A new `FDMonitor` class lives inside `healthmonitor.py`, alongside the existing `HealthMonitor` and `NotificationManager`. It is **not** a standalone process. Justification: the script already has notification plumbing (n8n → Pushover → Uptime Kuma), restart logic, cooldown/grace-period state, and runs as a managed systemd service. Forking a second process would duplicate all of that and double the operational surface for bugs and config drift.

The probe runs as part of the existing main loop in `HealthMonitor.run()`. For each cycle, after the existing `process_service` call for baserow, the FD probe also runs (only for baserow in v1).

### Process-tree resolution

Each cycle:
1. Resolve the container's host PID via `docker inspect --format '{{.State.Pid}}' propertyops-baserow`. PIDs change after every container restart — never cache.
2. Walk the process tree from that PID downward. Baserow forks gunicorn workers (BACKEND processes), celery workers (CELERY_WORKER, EXPORT_WORKER, BEAT_WORKER), and a Caddy supervisor — FD pressure is split across all of them.
3. Tree walk: read `/proc/<pid>/task/<tid>/children` (newline-separated child PIDs) recursively, OR use `os.listdir('/proc')` and filter by parent — implementation choice deferred to the plan, but the probe must cover the entire descendant tree, not just direct children.

### FD counting

For each PID in the tree:
- Count entries in `/proc/<pid>/fd/`. This is the kernel's authoritative count of open file descriptors for that process.
- Sum across the tree.
- Catch `FileNotFoundError` per PID and skip that PID — workers come and go mid-walk; this is normal, not a fault.
- Catch `PermissionError` and log once per cycle (shouldn't happen since healthmonitor runs as root, but defensive).

### Limit reading

Each cycle, also read `/proc/<root_pid>/limits` and parse the "Max open files" line. Use the **soft** value as the denominator for percent calculations. Justification: the soft limit is what the kernel enforces; the hard limit is the ceiling the process may raise to but does not by default. Reading live (vs. hardcoding 65535) keeps the probe correct if we ever bump the limit again.

### Frequency

Same cadence as the existing health checks: `CHECK_INTERVAL` from `config.env` (currently 30s). The probe is cheap — a few `readdir` syscalls per cycle.

### Thresholds

Two new keys in `config.env`, both expressed as integer percentages:
- `FD_WARN_PERCENT=70` (default)
- `FD_CRITICAL_PERCENT=90` (default)

Computed thresholds:
- `warn_threshold = soft_limit * FD_WARN_PERCENT / 100`
- `critical_threshold = soft_limit * FD_CRITICAL_PERCENT / 100`

At the current 65535 soft limit, that's warn at 45,874 and critical at 58,981.

### State machine

`FDMonitor` holds per-service state (baserow only in v1):
- `last_warning_fired: bool` — true after a warning fired, until hysteresis clears it
- `last_alert_count: int` — peak FD count when the last warning fired, for context in recovery messages

Transitions per cycle:
| Current count | Last warning fired? | Action |
|---|---|---|
| `< 50% of soft` | true | Clear `last_warning_fired`, fire `recovery` notification |
| `< warn_threshold` | false | No-op |
| `>= warn_threshold` and `< critical_threshold` | false | Fire `fd_warning`, set `last_warning_fired = true` |
| `>= warn_threshold` and `< critical_threshold` | true | No-op (hysteresis suppresses re-fire) |
| `>= critical_threshold` | any | Fire `fd_critical`, then call existing restart path |

### Restart path

When the critical threshold is crossed, route through the **existing** `_restart_service` and `_should_restart` machinery in `HealthMonitor`. This ensures:
- `max_restarts` budget is respected (no restart storms — already at 3 per 30 minutes)
- `cooldown_period` is respected (no thrashing)
- Emergency state is entered if the budget is exhausted (manual intervention required)
- The restart goes through the same dependency-check path (`_check_dependency_health`) so postgres/redis health is verified first

In other words: an FD-driven restart counts against the same budget as a 5xx-driven restart. Per design discussion, this is intentional — FD exhaustion is just another flavor of "baserow needs a kick."

To trigger the existing path cleanly, refactor the restart-decision logic out of `process_service` into a new method `HealthMonitor.request_restart(svc, reason: str) -> bool`. The existing 5xx-style failure path and the new FD-critical path both call `request_restart`. This avoids the FD probe having to fake `consecutive_failures` to coerce the existing code, and gives the restart logs a clear "reason" field for post-incident debugging. The `reason` string is passed through to the notification message ("baserow restart triggered: FD count 59,012 / 65,535 (90%)") so the operator immediately knows *why* a restart fired.

### Notifications

Two new `event_type` values:
- `fd_warning` — Pushover priority `0` (normal)
- `fd_critical` — Pushover priority `1` (high)

Both added to `PUSHOVER_PRIORITY_MAP`. The existing `NotificationManager.notify()` shape is unchanged; only the event_type strings are new. The n8n alert workflow reads `event_type` as a free-form string, so it does not need a code change to receive the new event types — but the workflow's branching logic should be reviewed so the new events get appropriate routing (out of scope for this spec; track as a follow-up in n8n).

### Recovery notification

When `last_warning_fired` clears (count drops below 50% of soft), fire a `recovery` event consistent with how the existing service-recovery path works. Message includes the peak count and the restart status if applicable.

## Edge cases and failure handling

| Condition | Behavior |
|---|---|
| Container is down (`docker inspect` returns empty PID) | Skip FD check this cycle. The existing `internal_url` health check will catch the outage and trigger restart via the existing path. |
| Process tree changes mid-walk (a worker exits) | Catch `FileNotFoundError` on the missing `/proc/<pid>/fd`, skip that PID, continue summing. |
| `/proc/<root_pid>/limits` unreadable | Log a warning, skip this cycle's FD check. |
| `docker inspect` times out or errors | Log a warning, skip this cycle's FD check. |
| FD count is 0 or absurdly low (<10) | Probably a race — log debug, skip the threshold check this cycle. |
| FD count exceeds the soft limit (shouldn't happen per kernel rules but be defensive) | Treat as `>= critical_threshold` and proceed. |

## Configuration

Additions to `docker/scripts/config.env`:
```
# FD monitoring (baserow only in v1)
FD_WARN_PERCENT=70
FD_CRITICAL_PERCENT=90
```

Defaults are coded in `load_config()` so the existing config.env continues to work without these keys (backward compatible — no break for the live deployment).

## Testing

### Unit tests
- FD-counting against a mocked `/proc` tree (use `tmp_path` to construct fake `/proc/<pid>/fd/` directories)
- Threshold calculation with various soft-limit values
- State-machine transitions (below warn → above warn → critical → recovery, with hysteresis edges)
- Process-tree walk handling missing PIDs mid-walk

### Integration test (manual, documented in the spec)
- Run the probe against the live baserow container and assert it returns a sane number (>0, < soft_limit, plausible given current load)

### End-to-end manual verification
- Temporarily set `FD_WARN_PERCENT` to a value below the current FD count (e.g., 1)
- Restart healthmonitor service
- Verify the warning notification fires (check n8n + Pushover)
- Set `FD_WARN_PERCENT` back to 70
- Verify recovery notification fires after hysteresis clears

## Open questions

- **Should the recovery notification be tier-1 (Pushover) or only via n8n?** Existing `recovery` events use priority `-1` (Pushover quiet). Recommend keeping consistent with that — out of scope to revisit.
- **Should the probe also log to a CSV/JSON timeseries on disk for later trend analysis?** Tempting, but adds another rotation/retention concern. Recommend deferring to Bucket 5 (centralized logs).

## Files to change

- `docker/scripts/healthmonitor.py` — add `FDMonitor` class; refactor `HealthMonitor.process_service` to expose `request_restart(reason)`; wire the FD probe into the main loop
- `docker/scripts/config.env` — add `FD_WARN_PERCENT` and `FD_CRITICAL_PERCENT` with documented defaults

No changes to:
- `docker/baserow/docker-compose.yml` (Bucket 1 already raised the FD limit)
- The systemd unit (no new dependencies, no env changes)
- The n8n workflow (event_type is free-form; new types route through the same handler)

## Rollout

1. Implement and unit-test in a feature branch
2. Deploy by updating `healthmonitor.py` + `config.env` on the host
3. Restart the systemd unit (`systemctl restart healthmonitor`)
4. Run the end-to-end manual verification (lower threshold, watch alerts fire, restore)
5. Monitor the first 24 hours for unexpected alert noise; tune thresholds if needed

---

## Addendum: Worker Recycling (2026-04-21, Bucket 3)

**Status:** Shipped

FDMonitor (Bucket 2) detects and reacts to FD exhaustion but doesn't prevent it. The root mechanism is gunicorn worker recycling via `--max-requests`.

### How it works

Gunicorn's `--max-requests N` causes each worker process to exit gracefully after handling N requests. The master process detects the exit and spawns a fresh replacement. The replacement starts with zero leaked FDs. This is a rolling recycle — with 3 workers, at most 1 exits at a time. No downtime.

### Configuration

Added to `docker/baserow/.env`:
```
GUNICORN_CMD_ARGS=--max-requests=500 --max-requests-jitter=50
```

`GUNICORN_CMD_ARGS` is a gunicorn-native env var read at Python startup from the process environment via `get_cmd_args_from_env()` in `gunicorn/app/base.py`. The flags do not appear in `ps aux` cmdline — they're applied to gunicorn's runtime config. Verify with:
```bash
docker exec propertyops-baserow env | grep GUNICORN_CMD_ARGS
```

### Threshold recalibration

FDMonitor thresholds tightened from 70%/90% to 40%/60% (in both `docker/scripts/config.env` and the hardcoded defaults in `load_config()` in `docker/scripts/healthmonitor.py`). With recycling in place, a 40% threshold crossing means recycling broke — a genuine signal requiring investigation.

### Architecture role after this change

| Layer | Role |
|-------|------|
| `--max-requests=500` | Primary prevention — workers shed FDs before accumulation |
| `ulimits: nofile: 65535` | Capacity buffer — delays failure mode by 64x if recycling breaks |
| FDMonitor at 40%/60% | Safety net — catches recycling failures, new leak paths, or Celery pressure |
