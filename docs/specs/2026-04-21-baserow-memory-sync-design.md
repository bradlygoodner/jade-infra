# Baserow ↔ Claude Code Memory Sync

**Status:** Design — awaiting user review
**Date:** 2026-04-21
**Owner:** Bradly (infra) + Claude
**Bucket:** Standalone — closes the cross-interface memory gap between Claude Code (VPS) and Claude Desktop / Claude.ai

## Background

Bradly operates the Jade Properties stack across three Claude interfaces: Claude Desktop, Claude.ai (web), and Claude Code (this CLI on VPS pct 300). Two parallel memory systems exist today:

1. **Baserow `claude-context` tables** (id 668 in jade-properties-group db, id 815 in coltons_plant_tracker db). Both have an identical 7-field schema (`key`, `title`, `category`, `last_updated`, `version`, `active`, `content`). Table 668 holds 10 active rows that Claude Desktop and Claude.ai use as their canonical cross-session memory: session-startup protocol, schema reference, append-only session log (currently v33), Bradly preferences, runbook index, code standards. An n8n workflow `yPaq1M8Bxcuxh3p7` (webhook `/update-context-row`) is the existing write path for Desktop. Row 8 (`07-bradly-preferences`) explicitly states: *"Biggest pain point: no cross-interface context — table 668 + CLAUDE.md are the solution."*

2. **Claude Code auto-memory** at `/root/.claude/projects/-root/memory/`. Seven Markdown files (`project_baserow_fd_leak.md`, `project_backup_retention.md`, `project_baserow_stack.md`, `project_plant_website.md`, `feedback_upgrade_script.md`, `reference_baserow_mcp.md`, `user_profile.md`) plus `MEMORY.md` index. Auto-loaded at session start in this CLI.

The two systems do not communicate. Work and decisions made in Claude Code do not appear in Baserow, so Desktop and Claude.ai sessions cannot see them; conversely, the curated context in table 668 (schema reference, session-startup protocol, build-plan workflow IDs) is invisible to Claude Code. The user has explicitly identified this as the highest-impact gap.

## Goal

Establish a two-way sync between Claude Code's local memory directory and the two `claude-context` tables, such that:

- Every Claude Code session begins with a fresh local mirror of both Baserow tables.
- Memory facts learned during a Claude Code session land in the appropriate Baserow table within the same turn (immediate push for fact rows).
- A single attributed entry per Claude Code session is appended to the existing `06-session-log` row at session end (batched push).
- Concurrent writes from Desktop or Claude.ai during a Claude Code session are detected via the existing `version` field and surfaced rather than silently overwritten.
- The user's existing curated rows (`00-session-startup`, `03-baserow-schema`, `06-session-log`, `07-bradly-preferences`, etc.) are never accidentally clobbered.

## Non-goals

- Replacing Claude Desktop's existing `/update-context-row` webhook path. Desktop continues to use it; Claude Code uses its own path. The two writers are independent peers writing to the same canonical store.
- Real-time cross-interface notifications ("Desktop just wrote to row 7"). The `version` freshness check at write time is the only synchronization primitive.
- Truncating or archiving the growing session-log row. Re-evaluate in three months once the actual growth rate is observed under multi-interface load.
- Migrating the existing local `user_profile.md` content into Baserow row 8 as part of this rollout. The mapping is established (file maps to row 8 via override), but the first push happens organically on the next user-profile update.
- Routing logic for any database other than Jade Ops (db 178) and Plants (db 217).

## Architecture

### Three sync points, three actors

```
┌─────────────────────────────────────────────────────────────┐
│  Claude Code (VPS pct 300)                                  │
│                                                             │
│  /root/.claude/projects/-root/memory/                       │
│   ├─ MEMORY.md                                              │
│   ├─ project_*.md, feedback_*.md, reference_*.md, user_*.md │
│   └─ baserow_pull/        ← read-only mirror of tables      │
│       ├─ jade_ops_<key>.md                                  │
│       ├─ plants_<key>.md                                    │
│       └─ .manifest.json                                     │
│                                                             │
│  /root/.claude/hooks/baserow-sync/state/                    │
│   ├─ session-log.txt      ← append during session           │
│   ├─ pending-pushes.json  ← MCP failures, retry next start  │
│   └─ last-stop-error.log  ← only present if last stop blew  │
└─────────────────────────────────────────────────────────────┘
        │                    │                    │
        │ SessionStart       │ in-session         │ Stop hook
        │ hook (PULL)        │ (PUSH facts)       │ (PUSH log)
        ▼                    ▼                    ▼
  bash 2-liner →        Claude via MCP      bash 2-liner →
   sync.py pull        (Jade Ops or Plants    sync.py stop
                        Baserow), version-
                        checked
        │                                         │
        ▼                                         ▼
┌──────────────────────────────────────────────────────────────┐
│  Baserow — app.jadepropertiesgroup.com                       │
│                                                              │
│  db 178 → claude-context table 668 (Jade Ops)                │
│   ├─ row 7 (06-session-log)  ← Stop hook prepends entries    │
│   ├─ rows for project_baserow_*, feedback_upgrade_script,   │
│   │   reference_baserow_mcp, etc.                            │
│   └─ existing curated rows untouched unless explicitly       │
│       overridden via frontmatter `baserow_key:`              │
│                                                              │
│  db 217 → claude-context table 815 (Plants)                  │
│   └─ rows for project_plant_website and future plants notes  │
└──────────────────────────────────────────────────────────────┘
```

The only thing Claude touches live is the local memory directory and Baserow MCP. Everything else is the harness running scripts. If the session crashes mid-way, in-session fact pushes are already done; only the session-log batch can be lost — but the local pending log file survives and the next session's SessionStart detects and flushes it.

### File-to-row mapping

Local memory files use the auto-memory frontmatter format:

```markdown
---
name: <human title>
description: <one-line>
type: project | feedback | reference | user
baserow_key: <optional override>
baserow_target: <optional: jade_ops | plants>
---

(body)
```

Mapping rules:

| Local field | Baserow field | Notes |
|---|---|---|
| filename minus `.md` | `key` (primary) | overridden by `baserow_key` if present |
| frontmatter `name` | `title` | |
| body after frontmatter | `content` | |
| derived from `type` | `category` | see table below |
| script-managed | `version` | +1 on each update; 1 on create |
| today (UTC date) | `last_updated` | |
| always `true` on write | `active` | |

Type → category:

| Local `type` | Baserow `category` | Rationale |
|---|---|---|
| `project` | `state` | matches existing pattern (`audit_decisions`, `audit_phase2_*` are `state`) |
| `feedback` | `protocol` | feedback rules ARE protocols ("never interpolate shell vars") |
| `reference` | `schema` | reference memories are pointers/lookups |
| `user` | `startup` | matches existing row 8 (`07-bradly-preferences`) which is `startup` |

### Routing — which Baserow

Body keyword scan determines target table. If the body mentions any of `plants`, `coltons_plant_tracker`, `seeds`, `garden`, `plant_website` → push to Plants db 217 / table 815. Otherwise → Jade Ops db 178 / table 668.

For ambiguous cases (body matches both contexts), the script errors out and asks the user to add an explicit `baserow_target: plants` (or `jade_ops`) to the frontmatter.

### Initial mapping for existing local files

| File | Pushed? | Where | Key |
|---|---|---|---|
| `project_baserow_fd_leak.md` | yes | 668 | `project_baserow_fd_leak` |
| `project_backup_retention.md` | yes | 668 | `project_backup_retention` |
| `project_baserow_stack.md` | yes | 668 | `project_baserow_stack` |
| `project_plant_website.md` | yes | 815 | `project_plant_website` |
| `feedback_upgrade_script.md` | yes | 668 | `feedback_upgrade_script` |
| `reference_baserow_mcp.md` | yes | 668 | `reference_baserow_mcp` |
| `user_profile.md` | yes | 668 | `07-bradly-preferences` (via `baserow_key:` override) |

These rows are not back-filled at rollout time. The first push for each happens organically on the next update to that file. This avoids a rollout-time bulk write that would inflate every row's version on day one.

## Components

### sync.py (single Python script)

All real logic lives in `/root/.claude/hooks/baserow-sync/sync.py`. Two subcommands:

- `python3 sync.py pull` — invoked by SessionStart hook
- `python3 sync.py stop` — invoked by Stop hook

Dependencies: `requests`, `python-frontmatter`. Installed once via `pip3 install -r requirements.txt`.

Reads `.env` (chmod 600) at startup:

```
BASEROW_BASE_URL=https://app.jadepropertiesgroup.com
BASEROW_TOKEN=<reuse existing token used by yPaq1M8Bxcuxh3p7>
JADE_OPS_TABLE_ID=668
PLANTS_TABLE_ID=815
SESSION_LOG_ROW_ID=7
```

Token reuse rationale: the existing token already has the required permissions and is in the user's secrets rotation. Minting a fresh dedicated token would add rotation surface for no security gain (Claude Code on the VPS is not a less-trusted caller than the n8n workflow on the same VPS).

### pull.sh and stop.sh

Two-line bash wrappers, one per hook. They source `.env` and exec the Python script. Kept as separate files (vs inline in `settings.json`) so the hook command stays stable while the script can be edited.

### SessionStart hook (PULL)

Behavior:

1. `GET /api/database/rows/table/668/?user_field_names=true&size=200` and same for 815.
2. For each row: write `/root/.claude/projects/-root/memory/baserow_pull/<source>_<key>.md` with frontmatter recording `source`, `table`, `key`, `title`, `category`, `version`, `last_updated`, `pulled_at`. Body is the row's `content` field.
3. Write `baserow_pull/.manifest.json` mapping `key → {table, version, last_updated, row_id}` for fast lookup by in-session push code.
4. Detect orphans: any file in `baserow_pull/` with no matching active row gets deleted (row was deactivated or removed).
5. If `state/session-log.txt` exists from a prior crashed session, prepend a `[recovered from incomplete prior session]` marker and flush it now (run the same logic as `stop` against it, then truncate).
6. If `state/pending-pushes.json` exists, attempt the queued pushes one by one.
7. Exit 0 on success. On any failure (network, auth, malformed response), exit 0 anyway with a stderr warning. Stale local files are kept; Claude works from them.

Awareness mechanism: the SessionStart hook also writes a one-line stdout status (e.g. `Baserow pulled: 12 rows from 668, 3 from 815. See /root/.claude/projects/-root/memory/baserow_pull/`) so Claude sees confirmation in the transcript. A durable entry in `MEMORY.md` (`- [Baserow ops context](baserow_pull/) — pulled at session start; canonical jade-ops + plants state`) ensures Claude knows the directory exists across all future sessions.

### In-session push (facts, via Baserow MCP)

Trigger: any save or update to a non-`baserow_pull/` file under `/root/.claude/projects/-root/memory/` with frontmatter `type` of `project`, `feedback`, `reference`, or `user`.

Per-push procedure (Claude runs this via MCP in the same turn as the file edit):

1. Parse the local file's frontmatter and body.
2. Determine target table (routing rule above, or `baserow_target` override).
3. Determine key (frontmatter `baserow_key` if present, else filename without `.md`).
4. Look up the existing row in `baserow_pull/.manifest.json`.
5. Branch:
   - **Row absent in manifest** → MCP `create_rows` with `{key, title, content, category, version: 1, last_updated: today, active: true}`.
   - **Row present, manifest version is current** → MCP `update_rows` with `{id, content, title, category, version: pulled+1, last_updated: today}`.
   - **Row present, MCP `list_table_rows` shows current version > manifest version** → STOP. Surface the conflict to the user with three options: (a) overwrite anyway, (b) re-pull and merge by hand, (c) skip the push.
6. On success, update the local `.manifest.json` with the new version and `last_updated`.

The manifest is the source of truth for version comparison, not the local file's pull-side frontmatter. If the user hand-edits a `baserow_pull/` file, the manifest still reflects what was last seen on the wire.

Failure modes:
- MCP unreachable → Claude reports the failed file to the user and queues the push intent in `state/pending-pushes.json`. Next session's SessionStart attempts queued pushes first.
- Conflict detected → never auto-resolved. Always surfaced.

The MCP version of `update_rows` does not expose Baserow's optimistic-locking headers, so the version check has a sub-second read-then-write race window. Acceptable for the realistic concurrency level (Bradly + Colton + Claude across three interfaces). If concurrency increases materially, switch in-session pushes to the REST API and use `If-Match` headers.

### Stop hook (session-log push)

During the session, Claude appends one-line entries to `/root/.claude/hooks/baserow-sync/state/session-log.txt` via Bash whenever significant ops work occurs. Format:

```
2026-04-21T03:14:22Z|task|Debugged Redis AOF persistence — root cause: appendonly disabled in compose
2026-04-21T03:18:01Z|file|docker/baserow/docker-compose.yml — added appendonly yes
2026-04-21T03:22:45Z|decision|Keep AOF on; risk of data loss outweighs ~5% I/O cost
2026-04-21T03:30:11Z|bug|FD leak still recurring after restart — see project_baserow_fd_leak
```

Categories: `task`, `file`, `decision`, `bug`, `built`, `note`. Mirrors what Desktop's existing log entries already use. "Significant ops work" is judgment, same as the existing rule for what's worth saving to memory at all — quick reads, throwaway greps, exploratory questions are not logged.

At session end, the Stop hook runs `python3 sync.py stop`:

1. Read `state/session-log.txt`. If empty → exit 0.
2. Group entries into one block matching the existing row 7 format:
   ```
   2026-04-21 | Claude Code (VPS) | session-<short-id>
   Tasks: <bulleted lines from "task" entries>
   Bugs: <bug entries>
   Files changed: <file entries, deduped>
   Decisions: <decision entries>
   Built: <built entries>
   Notes: <note entries>
   ```
   Empty sections are omitted. Session-id is the first 8 chars of `$CLAUDE_SESSION_ID`.
3. `GET row 7` via REST. Compare returned `version` to `.manifest.json`.
4. **If no conflict:** prepend the new block + `\n---\n` separator to the existing `content`, `PATCH /api/database/rows/table/668/<row-id>/` with bumped `version` and today's `last_updated`.
5. **If conflict** (Desktop appended during this session): re-fetch row 7 and prepend the new block to that fresh content. This is safe because the log is append-only — we just want the new block at the top regardless of what was added below since the session-start pull.
6. Truncate `state/session-log.txt`.
7. On any failure: leave `state/session-log.txt` intact, write `state/last-stop-error.log` with the error. Next session's SessionStart sees the leftover file and flushes it.

### settings.json hook registration

Additive — does not disturb existing hooks:

```json
{
  "hooks": {
    "SessionStart": [
      { "matcher": "*", "hooks": [{ "type": "command", "command": "/root/.claude/hooks/baserow-sync/pull.sh" }] }
    ],
    "Stop": [
      { "matcher": "*", "hooks": [{ "type": "command", "command": "/root/.claude/hooks/baserow-sync/stop.sh" }] }
    ]
  }
}
```

Hook timeout default is 60s; pull and stop both target 1–2s. Well within budget.

### File layout

```
/root/.claude/
├─ settings.json                          ← register hooks here
└─ hooks/baserow-sync/
   ├─ sync.py                             ← all logic
   ├─ pull.sh                             ← 2-line wrapper for SessionStart
   ├─ stop.sh                             ← 2-line wrapper for Stop
   ├─ .env                                ← chmod 600
   ├─ requirements.txt                    ← requests, python-frontmatter
   ├─ tests/
   │  └─ test_sync.py
   └─ state/
      ├─ session-log.txt                  ← append-only during session
      ├─ pending-pushes.json              ← MCP failures from prior session
      └─ last-stop-error.log              ← only present if last stop failed
```

## Error handling

| Failure | Behavior | Visibility |
|---|---|---|
| SessionStart pull: network down | exit 0, stderr warning, work from stale local mirror | hook stderr in transcript |
| SessionStart pull: auth 401 | exit 0, stderr warning naming the file to check (`.env`) | hook stderr in transcript |
| SessionStart pull: leftover `session-log.txt` | recover-flush before this session starts | one-line stdout note |
| In-session push: MCP call fails | queue intent in `pending-pushes.json`, report to user | Claude reports in turn |
| In-session push: version conflict | hard stop, ask user (overwrite / re-pull / skip) | Claude asks directly |
| Stop push: row 7 fetch fails | leave `session-log.txt` intact, write `last-stop-error.log` | next-session announce |
| Stop push: payload malformed | same + write malformed payload to `state/last-failed-payload.txt` | next-session announce |
| Conflict during stop push | re-fetch + prepend (safe by design) | silent |

## Testing

- `tests/test_sync.py` covering: frontmatter parse, type→category mapping, key derivation, override resolution, routing keyword scan, conflict detection branching, log block formatting, recovery flow.
- `--dry-run` flag for `sync.py` that does GET/PATCH planning but logs instead of writing. Runnable by hand against a sandbox table; do not run against table 668 in prod.
- Manual E2E: save a test memory file → push runs → row appears in Baserow UI → next session's pull mirrors the file into `baserow_pull/`.

## Rollout

1. Build script + tests in a feature branch on the VPS.
2. Drop a test row in a scratch table first (not 668), confirm round-trip.
3. Enable hooks via `settings.json` only after manual verification passes.
4. First "real" run: trivial memory edit (e.g., update `project_baserow_stack.md`) and watch the row in Baserow UI to confirm push lands cleanly.
5. Watch for a few sessions. The hooks are one line away from being commented out if anything goes wrong.

## Operational concerns deferred

- **Session-log row growth.** Row 7 is currently 8KB at v33. New entries average ~5KB; six months out it'll be 100KB+. Baserow long_text has no hard cap that's been hit but editor usability degrades. Re-evaluate in three months once Claude Code's contribution to the growth rate is observable. Mitigation if needed: trim to last 50KB on push, archive trimmed entries to a separate `06-session-log-archive` row.

## Open questions

None. All design decisions resolved.

**Resolved during review — user_profile.md content shape:**
`user_profile.md` is currently much smaller than row 8 (`07-bradly-preferences`). The rollout step for this file is: expand `user_profile.md` to be a full superset of row 8's existing content before the first push. Standard `update_rows` semantics apply; no special cases in `sync.py`. One-time setup task covered in the implementation plan.
