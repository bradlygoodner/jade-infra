# Health Alert Workflow — n8n Build Guide

## Overview

This workflow receives health alerts from the Python health monitor (`healthmonitor.py`) running on the host. The monitor POSTs JSON to `http://localhost:5678/webhook/health-alert` whenever a service changes state (fails, restarts, recovers, enters emergency).

The workflow does two things:
1. **All events:** Log to a Baserow "Service Health" table and send a Pushover notification
2. **Emergency events only:** Gather Docker logs + system state, send to Claude for diagnosis, append the diagnosis to the Baserow row, and send an enriched emergency Pushover alert

## Prerequisites

Before building this workflow, you need:

1. **Baserow "Service Health" table** (see Step 1 below)
2. **Pushover credentials** configured in n8n (Settings > Credentials > Add Credential > Pushover API)
3. **Anthropic API key** configured in n8n as a Header Auth credential (for Claude API calls)
4. **Baserow API token** configured in n8n (Settings > Credentials > Add Credential > Baserow API)

## Incoming Webhook Payload

Every POST from the health monitor has this exact shape:

```json
{
  "service": "baserow",
  "status": "unhealthy",
  "event_type": "restart_initiated",
  "message": "baserow failed 2 consecutive health checks. Restarting (attempt 1/3).",
  "timestamp": "2026-04-07T03:15:00+00:00",
  "restart_count": 1,
  "check_type": "internal"
}
```

**Field details:**

| Field | Type | Possible Values |
|-------|------|-----------------|
| `service` | string | `baserow`, `n8n`, `docuseal` |
| `status` | string | `unhealthy`, `restarting`, `healthy`, `emergency`, `tunnel_issue` |
| `event_type` | string | `failure_detected`, `restart_initiated`, `restart_success`, `restart_failed`, `emergency`, `recovery`, `tunnel_issue` |
| `message` | string | Human-readable description of what happened |
| `timestamp` | string | ISO 8601 UTC timestamp |
| `restart_count` | number | How many restarts have been attempted in the current window |
| `check_type` | string | `internal` (localhost check) or `public` (Cloudflare tunnel check) |

---

## Step 1: Create the Baserow Table

In Baserow, create a new table called **Service Health** in your existing database.

Add these fields in order (the first "Name" field Baserow creates automatically — you can delete it or rename it):

| # | Field Name | Field Type | Configuration |
|---|-----------|------------|---------------|
| 1 | Timestamp | Date | Enable "Include time", format: ISO |
| 2 | Service | Single Select | Add options: `baserow`, `n8n`, `docuseal` |
| 3 | Event Type | Single Select | Add options: `failure_detected`, `restart_initiated`, `restart_success`, `restart_failed`, `emergency`, `recovery`, `tunnel_issue` |
| 4 | Check Type | Single Select | Add options: `internal`, `public` |
| 5 | Restart Count | Number | Integer, no decimals |
| 6 | Message | Long Text | No special config |
| 7 | LLM Diagnosis | Long Text | No special config — only populated for emergency events |
| 8 | Resolved | Boolean | Default: unchecked |
| 9 | Resolution Notes | Long Text | For your manual post-incident notes |

After creating the table, note the **Table ID** — you'll need it when configuring the Baserow nodes. You can find it in the URL when viewing the table: `https://app.jadepropertiesgroup.com/database/TABLE_ID/...`

---

## Step 2: Build the Workflow

Create a new workflow in n8n called **Service Health Monitor**.

### Node 1: Webhook (Trigger)

1. Add a **Webhook** node
2. Configure:
   - **HTTP Method:** POST
   - **Path:** `health-alert`
   - **Authentication:** None
   - **Response Mode:** "Immediately" (the health monitor doesn't wait for the response)
3. This is the entry point. The health monitor POSTs to `http://localhost:5678/webhook/health-alert`

### Node 2: Switch (Route by Event Type)

1. Add a **Switch** node, connect it to the Webhook output
2. Configure:
   - **Mode:** Rules
   - **Data Type:** String
   - **Value:** `{{ $json.event_type }}`
   - **Rule 1:**
     - Operation: `equals`
     - Value: `emergency`
     - Output: 0 (this goes to the emergency branch)
   - **Fallback Output:** 1 (this goes to the normal log+notify branch)

The Switch node creates two outputs. Output 0 = emergency, Output 1 = everything else.

### Node 3: Baserow — Create Row (connects to BOTH Switch outputs)

This node logs every event to Baserow. Connect it to **both** outputs of the Switch node (Output 0 AND Output 1).

1. Add a **Baserow** node
2. Configure:
   - **Credential:** Your Baserow API credential
   - **Operation:** Create Row
   - **Database ID:** Your database ID
   - **Table ID:** The Service Health table ID from Step 1
   - **Field mapping** (set each field):
     - **Timestamp:** `{{ $json.timestamp }}`
     - **Service:** `{{ $json.service }}`
     - **Event Type:** `{{ $json.event_type }}`
     - **Check Type:** `{{ $json.check_type }}`
     - **Restart Count:** `{{ $json.restart_count }}`
     - **Message:** `{{ $json.message }}`
     - **Resolved:** `false`

**Important:** This node's output includes the created row's `id` — you'll need this in the emergency branch to update the row with the LLM diagnosis later.

### Node 4: Pushover — Send Notification (connects to Baserow output)

1. Add a **Pushover** node, connect it to the Baserow node output
2. Configure:
   - **Credential:** Your Pushover API credential
   - **Title:** `PropertyOps: {{ $json.service }}`  
     - Note: After the Baserow node, the original webhook data is in `$('Switch').item.json` or you may need to reference it via the Webhook node directly: `{{ $('Webhook').item.json.service }}`
   - **Message:** `{{ $('Webhook').item.json.message }}`
   - **Priority:** You need to set this dynamically. Use an **IF** or **Switch** before this node, OR use the Expression editor:
     ```
     {{ $('Webhook').item.json.event_type === 'emergency' ? 2 : ($('Webhook').item.json.event_type === 'recovery' ? -1 : 0) }}
     ```
   - For priority 2 (emergency), also set:
     - **Retry:** 60
     - **Expire:** 3600

**For the normal branch (Switch Output 1), this is the end of the flow.**

### Node 5: Execute Command — Gather Logs (emergency branch only)

This node only runs for emergency events. Connect it to the **Baserow node output, but only on the emergency path** (from Switch Output 0 -> Baserow -> this node).

1. Add an **Execute Command** node
2. Configure the command:

```bash
echo "=== CONTAINER STATE ==="
docker inspect --format='{{json .State}}' propertyops-{{ $('Webhook').item.json.service }} 2>&1 | python3 -m json.tool

echo ""
echo "=== LAST 200 LOG LINES ==="
docker logs --tail 200 --timestamps propertyops-{{ $('Webhook').item.json.service }} 2>&1

echo ""
echo "=== DISK USAGE ==="
df -h / /docker 2>/dev/null

echo ""
echo "=== MEMORY ==="
free -m

echo ""
echo "=== DOCKER STATS SNAPSHOT ==="
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}" 2>&1
```

**Note on n8n Execute Command:** n8n runs as user 1000:1000 inside its container, so it does NOT have access to the Docker socket. You have two options:

**Option A (Recommended): Use SSH to the host**
- Instead of Execute Command, use an **SSH** node
- Connect to `host.docker.internal` or the host's IP
- Run the commands as root on the host
- This requires setting up SSH key auth from the n8n container to the host

**Option B: Mount the Docker socket into n8n**  
- Add to n8n's docker-compose.yml: `- /var/run/docker.sock:/var/run/docker.sock`
- This gives n8n full Docker access (security consideration)
- Also need to install `docker` CLI inside the n8n container

**Option C: Use a helper script on the host**
- Create a script `/root/docker/scripts/gather-diagnostics.sh` that writes output to a shared volume
- n8n reads the file from its mounted `/backups` volume
- Trigger the script via a webhook or file watcher

Pick the option that fits your security comfort level. Option A is most secure. The command output is what gets sent to Claude for diagnosis.

### Node 6: HTTP Request — Claude API (emergency branch)

1. Add an **HTTP Request** node, connect it to the Execute Command/SSH output
2. Configure:
   - **Method:** POST
   - **URL:** `https://api.anthropic.com/v1/messages`
   - **Authentication:** Predefined Credential Type > Header Auth
     - Header Name: `x-api-key`  
     - Header Value: Your Anthropic API key
   - **Send Headers:** Yes
     - `anthropic-version`: `2023-06-01`
     - `content-type`: `application/json`
   - **Send Body:** Yes, JSON
   - **Body (JSON):**

```json
{
  "model": "claude-sonnet-4-6-20250514",
  "max_tokens": 1024,
  "messages": [
    {
      "role": "user",
      "content": "You are a DevOps engineer diagnosing a Docker service failure on a production server.\n\nService: {{ $('Webhook').item.json.service }}\nEvent: {{ $('Webhook').item.json.message }}\n\nThe service has failed 3 automatic restart attempts by our health monitor. Here are the diagnostic logs and system state:\n\n{{ $json.stdout }}\n\nBased on these logs, provide:\n1. Most probable root cause (be specific, cite log lines)\n2. Top 3 remediation steps in order of likelihood to fix the issue\n3. Any data or state that should be backed up before taking action\n4. Whether this looks like a transient issue or a persistent problem\n\nBe concise and actionable. The person reading this is being woken up at 2 AM."
    }
  ]
}
```

The response body will be at `$json.content[0].text` — this is Claude's diagnosis.

### Node 7: Baserow — Update Row (emergency branch)

1. Add a **Baserow** node, connect it to the HTTP Request output
2. Configure:
   - **Operation:** Update Row
   - **Table ID:** Same Service Health table
   - **Row ID:** `{{ $('Baserow').item.json.id }}`  
     (This references the row created in Node 3. The node name may vary — use whatever you named the first Baserow node.)
   - **Fields to update:**
     - **LLM Diagnosis:** `{{ $json.content[0].text }}`

### Node 8: Pushover — Emergency Alert with Diagnosis (emergency branch)

1. Add a **Pushover** node, connect it to the Baserow Update output
2. Configure:
   - **Title:** `EMERGENCY: {{ $('Webhook').item.json.service }}`
   - **Message:**
     ```
     {{ $('Webhook').item.json.service }} failed 3 restart attempts.

     Diagnosis: {{ $('HTTP Request').item.json.content[0].text }}
     ```
     (Truncate if needed — Pushover has a 1024 character message limit. You can use `.substring(0, 900)` in the expression.)
   - **Priority:** 2 (Emergency)
   - **Retry:** 60
   - **Expire:** 3600
   - **Sound:** `siren` or `alien` (something attention-grabbing for 2 AM)

---

## Final Workflow Layout

```
                                    +---> [Baserow: Create Row] ---> [Pushover: Notify]
                                    |         (normal events)
[Webhook] ---> [Switch] ---+        |
                           |        |
                           +--------+
                           |
                           +---> [Baserow: Create Row] ---> [Execute Cmd / SSH] ---> [Claude API] ---> [Baserow: Update Row] ---> [Pushover: Emergency]
                                    (emergency events)
```

**Simplified view:** The Baserow Create Row node is shared — both branches go through it. Then normal events just get a Pushover notification, while emergency events continue through the diagnostic pipeline.

If it's easier, you can duplicate the Baserow Create Row node so each branch has its own copy. That avoids complex node connection routing.

---

## Step 3: Test the Workflow

### Activate the workflow first

Click "Active" toggle in n8n to enable the webhook endpoint.

### Test 1: Normal alert

Run this from the server terminal:

```bash
curl -X POST http://localhost:5678/webhook/health-alert \
  -H "Content-Type: application/json" \
  -d '{
    "service": "baserow",
    "status": "unhealthy",
    "event_type": "restart_initiated",
    "message": "Test: baserow failed 2 consecutive health checks. Restarting (attempt 1/3).",
    "timestamp": "2026-04-07T12:00:00Z",
    "restart_count": 1,
    "check_type": "internal"
  }'
```

**Verify:**
- New row in Baserow Service Health table with all fields populated
- Pushover notification on your phone with title "PropertyOps: baserow"

### Test 2: Recovery event

```bash
curl -X POST http://localhost:5678/webhook/health-alert \
  -H "Content-Type: application/json" \
  -d '{
    "service": "baserow",
    "status": "healthy",
    "event_type": "recovery",
    "message": "Test: baserow has recovered and is healthy again.",
    "timestamp": "2026-04-07T12:05:00Z",
    "restart_count": 0,
    "check_type": "internal"
  }'
```

**Verify:**
- New row in Baserow with Event Type = recovery
- Pushover notification with low priority (should be silent/no sound)

### Test 3: Emergency with LLM diagnosis

```bash
curl -X POST http://localhost:5678/webhook/health-alert \
  -H "Content-Type: application/json" \
  -d '{
    "service": "baserow",
    "status": "emergency",
    "event_type": "emergency",
    "message": "Test: baserow failed 3 restart attempts in 30 minutes. Manual intervention required.",
    "timestamp": "2026-04-07T12:10:00Z",
    "restart_count": 3,
    "check_type": "internal"
  }'
```

**Verify:**
- New row in Baserow with Event Type = emergency
- LLM Diagnosis field populated with Claude's analysis
- Emergency Pushover notification (loud, requires acknowledgment) with diagnosis summary

### Test 4: Tunnel issue

```bash
curl -X POST http://localhost:5678/webhook/health-alert \
  -H "Content-Type: application/json" \
  -d '{
    "service": "n8n",
    "status": "tunnel_issue",
    "event_type": "tunnel_issue",
    "message": "Test: n8n is healthy internally but unreachable via public URL. Cloudflare tunnel may be down.",
    "timestamp": "2026-04-07T12:15:00Z",
    "restart_count": 0,
    "check_type": "public"
  }'
```

**Verify:**
- Row in Baserow, Pushover notification about tunnel issue

---

## Troubleshooting

**Webhook returns 404:** The workflow is not active. Toggle it on in n8n.

**Baserow node fails:** Check that the Table ID and field names match exactly. Field names are case-sensitive. Make sure the Baserow API credential has write access.

**Claude API returns 401:** Check the `x-api-key` header auth credential. Make sure the `anthropic-version` header is set to `2023-06-01`.

**Claude API returns 400:** The body JSON is malformed. Check that the expression references (`$('Webhook').item.json.service`) resolve correctly. Test with hardcoded values first.

**Pushover not sending:** Verify the Pushover credential in n8n. Test with a simple Pushover node first before wiring it into the workflow.

**Execute Command has no Docker access:** See Option A/B/C in Node 5 above. n8n runs as a non-root user inside Docker and cannot access the Docker socket by default.

---

## After Setup

Once the workflow is active and tested, the health monitor will automatically route alerts through it. You'll see:

- **Normal operations:** Occasional rows in Baserow for restarts/recoveries, Pushover notifications
- **Emergencies:** Full diagnostic pipeline with Claude analysis delivered to your phone
- **Tunnel issues:** Alerts about Cloudflare connectivity without unnecessary restarts

You can filter and sort the Baserow table to see patterns — which services fail most, what time of day, etc. Mark rows as "Resolved" and add Resolution Notes after you investigate.
