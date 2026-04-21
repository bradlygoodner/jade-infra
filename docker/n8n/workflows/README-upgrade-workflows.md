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

**Note on file access:** n8n runs inside a container. For it to read backup files from the host, the backup directory is mounted into the n8n container at `/backups` (read-only). Update the Code node paths to reference `/backups/` instead of `/root/docker/backups/`.

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
