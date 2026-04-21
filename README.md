# jade-ops

Homelab infrastructure for Jade Properties Group. Docker stacks, health monitor, backup/upgrade automation, and design docs for a self-hosted Proxmox VM running Baserow, n8n, and DocuSeal.

## Layout

```
jade-ops/
├── docker/         # docker-compose stacks + .env.example per service
├── scripts/        # backup.sh, upgrade.sh, rollback.sh, healthmonitor.py
├── hooks/          # Claude Code hooks (baserow-sync)
├── systemd/        # healthmonitor service unit (installed via install-systemd.sh)
└── docs/           # superpowers specs + plans, workflow runbooks
```

Runtime state (DBs, logs, backups, container volumes) lives **outside** this repo — intentional, so git never tracks it:

- `/root/docker/backups/` — backup archives
- `/root/docker/logs/` — healthmonitor + backup + upgrade logs
- `/root/docker/volumes/` — n8n and docuseal data
- `/docker/volumes/propertyops/` — baserow postgres/redis/media

## Quickstart (fresh deployment)

1. Clone this repo to `/opt/jade-infra`.
2. For each stack, copy the `.env.example` to `.env` and fill in real values:
   ```bash
   for stack in docker/baserow docker/n8n docker/docuseal; do
     cp /opt/jade-infra/$stack/.env.example /opt/jade-infra/$stack/.env
     chmod 600 /opt/jade-infra/$stack/.env
     $EDITOR /opt/jade-infra/$stack/.env
   done
   ```
3. Create the shared config for the ops scripts:
   ```bash
   cp /opt/jade-infra/scripts/config.env.example /opt/jade-infra/scripts/config.env
   chmod 600 /opt/jade-infra/scripts/config.env
   $EDITOR /opt/jade-infra/scripts/config.env   # fill Pushover + Uptime Kuma
   ```
4. Pre-create runtime dirs (see paths in `scripts/config.env`): `mkdir -p /root/docker/{backups,logs,volumes} /docker/volumes/propertyops/{postgres,redis,baserow}`
5. Pull images and start stacks:
   ```bash
   docker compose -f docker/baserow/docker-compose.yml up -d
   docker compose -f docker/n8n/docker-compose.yml up -d
   docker compose -f docker/docuseal/docker-compose.yml up -d
   docker compose -f docker/watchtower/docker-compose.yml up -d
   ```
6. Install the health monitor:
   ```bash
   bash /opt/jade-infra/scripts/install-systemd.sh
   systemctl enable propertyops-healthmonitor
   ```
7. Register cron jobs:
   ```cron
   0 3 * * * /opt/jade-infra/scripts/backup.sh  >> /root/docker/logs/backup.log  2>&1
   0 2 * * 0 /opt/jade-infra/scripts/upgrade.sh >> /root/docker/logs/upgrade.log 2>&1
   ```

## Baserow-sync hook

The `hooks/baserow-sync/` directory is a Claude Code hook that syncs memory files to a Baserow table. Claude Code discovers hooks at `~/.claude/hooks/`, so on this server the path is wired in via symlink:

```bash
ln -s /opt/jade-infra/hooks/baserow-sync /root/.claude/hooks/baserow-sync
```

Install dependencies with `pip install -r hooks/baserow-sync/requirements.txt`.

## Running tests

```bash
cd /opt/jade-infra/scripts && python3 -m pytest tests/ -v
cd /opt/jade-infra/hooks/baserow-sync && python3 -m pytest tests/ -v
```

## Service topology reference

| Service | Port | Data path | Notes |
|---------|------|-----------|-------|
| baserow | 8086 | `/docker/volumes/propertyops/` | Front-door DB; uploads via Cloudflare tunnel |
| n8n | 5678 | `/root/docker/volumes/n8n/` | Automations; workflow runbooks in `docs/n8n-workflows/` |
| docuseal | 3001 | `/root/docker/volumes/docuseal/` | Document signing |
| watchtower | — | — | Auto-updates other images (monitoring only, no persistent data) |
| healthmonitor | — | logs in `/root/docker/logs/` | Systemd service; monitors HTTP health + FDs + disk |

## Related reading

- `docs/superpowers/specs/` — design documents
- `docs/superpowers/plans/` — implementation plans, including tonight's gunicorn-recycling + FDMonitor work (`2026-04-21-baserow-fd-leak-prevention.md`)
- `docs/n8n-workflows/` — workflow-level runbooks (health-alert workflow, etc.)
