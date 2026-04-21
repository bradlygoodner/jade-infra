# Baserow Memory Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement two-way sync between Claude Code's local auto-memory directory and the Baserow `claude-context` tables (668 jade-ops, 815 plants) via Claude Code hooks and direct Baserow REST API calls.

**Architecture:** A single Python script (`sync.py`) handles both `pull` (SessionStart hook fetches all Baserow rows into `memory/baserow_pull/`) and `stop` (Stop hook prepends a session-log entry to row 7). In-session fact pushes are performed by Claude directly via Baserow MCP — not by the script. Conflict detection uses the `version` field tracked in a local `.manifest.json`.

**Tech Stack:** Python 3.10, `requests`, `python-frontmatter`, Baserow REST API (`/api/database/rows/table/<id>/`), Claude Code hooks (`SessionStart`, `Stop`).

---

## File Map

| Path | Action | Purpose |
|---|---|---|
| `/root/.claude/hooks/baserow-sync/sync.py` | Create | All pull/stop logic + pure utility functions |
| `/root/.claude/hooks/baserow-sync/tests/test_sync.py` | Create | Unit + integration-mock tests |
| `/root/.claude/hooks/baserow-sync/pull.sh` | Create | 4-line SessionStart hook wrapper |
| `/root/.claude/hooks/baserow-sync/stop.sh` | Create | 4-line Stop hook wrapper |
| `/root/.claude/hooks/baserow-sync/.env` | Create | Secrets (chmod 600) |
| `/root/.claude/hooks/baserow-sync/requirements.txt` | Create | `requests`, `python-frontmatter` |
| `/root/.claude/settings.json` | Modify | Register SessionStart + Stop hooks |
| `/root/.claude/projects/-root/memory/MEMORY.md` | Modify | Add baserow_pull pointer + in-session push protocol |
| `/root/.claude/projects/-root/memory/user_profile.md` | Modify | Expand to superset of row 8 content |

State files created at runtime by `sync.py` (no manual setup needed):
- `state/session-log.txt` — append-only during session
- `state/pending-pushes.json` — MCP push failures for retry
- `state/last-stop-error.log` — only present after a stop failure
- `memory/baserow_pull/.manifest.json` — version manifest

---

## Task 1: Project Scaffolding

**Files:**
- Create: `/root/.claude/hooks/baserow-sync/sync.py`
- Create: `/root/.claude/hooks/baserow-sync/tests/test_sync.py`
- Create: `/root/.claude/hooks/baserow-sync/requirements.txt`
- Create: `/root/.claude/hooks/baserow-sync/.env`

- [ ] **Step 1: Create directory structure**

```bash
mkdir -p /root/.claude/hooks/baserow-sync/tests
mkdir -p /root/.claude/hooks/baserow-sync/state
```

- [ ] **Step 2: Create requirements.txt**

```
/root/.claude/hooks/baserow-sync/requirements.txt
```
```
requests
python-frontmatter
```

- [ ] **Step 3: Install dependencies**

```bash
pip3 install -r /root/.claude/hooks/baserow-sync/requirements.txt
```

Expected output ends with: `Successfully installed ...` (no errors)

- [ ] **Step 4: Create .env with placeholders**

```
/root/.claude/hooks/baserow-sync/.env
```
```
BASEROW_BASE_URL=https://app.jadepropertiesgroup.com
BASEROW_TOKEN=FILL_IN_TOKEN_FROM_N8N_WORKFLOW_yPaq1M8Bxcuxh3p7
JADE_OPS_TABLE_ID=668
PLANTS_TABLE_ID=815
SESSION_LOG_ROW_ID=7
```

Then lock the file:

```bash
chmod 600 /root/.claude/hooks/baserow-sync/.env
```

**Token location:** In n8n, open workflow `yPaq1M8Bxcuxh3p7` (Context Updater). The Baserow API token is in the HTTP Request node's Authorization header. Copy it into `.env`.

- [ ] **Step 5: Create empty sync.py stub**

```python
#!/usr/bin/env python3
"""Baserow <-> Claude Code memory sync."""
```

```bash
chmod +x /root/.claude/hooks/baserow-sync/sync.py
```

- [ ] **Step 6: Create empty test file**

```python
# /root/.claude/hooks/baserow-sync/tests/test_sync.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
```

- [ ] **Step 7: Verify pytest can discover the test file**

```bash
cd /root/.claude/hooks/baserow-sync && python3 -m pytest tests/ -v
```

Expected: `no tests ran` (or `0 passed`) with exit code 0 or 5.

- [ ] **Step 8: Commit scaffolding**

```bash
git -C /root add /root/.claude/hooks/baserow-sync/
git -C /root commit -m "feat: scaffold baserow-sync hook directory"
```

---

## Task 2: Pure Functions — Tests then Implementation

**Files:**
- Modify: `/root/.claude/hooks/baserow-sync/tests/test_sync.py`
- Modify: `/root/.claude/hooks/baserow-sync/sync.py`

Pure functions have no I/O — no HTTP, no disk. Fast, deterministic, no mocking needed.

- [ ] **Step 1: Write failing tests for pure functions**

Replace the contents of `tests/test_sync.py`:

```python
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import sync


# --- derive_key ---

def test_derive_key_uses_filename_by_default(tmp_path):
    f = tmp_path / "project_baserow_fd_leak.md"
    assert sync.derive_key(f, {}) == "project_baserow_fd_leak"


def test_derive_key_uses_frontmatter_override(tmp_path):
    f = tmp_path / "user_profile.md"
    assert sync.derive_key(f, {"baserow_key": "07-bradly-preferences"}) == "07-bradly-preferences"


# --- type_to_category ---

def test_type_to_category_project():
    assert sync.type_to_category("project") == "state"


def test_type_to_category_feedback():
    assert sync.type_to_category("feedback") == "protocol"


def test_type_to_category_reference():
    assert sync.type_to_category("reference") == "schema"


def test_type_to_category_user():
    assert sync.type_to_category("user") == "startup"


def test_type_to_category_unknown_defaults_to_state():
    assert sync.type_to_category("something_weird") == "state"


# --- extract_category_value ---

def test_extract_category_value_from_baserow_dict():
    assert sync.extract_category_value({"id": 1, "value": "state", "color": "red"}) == "state"


def test_extract_category_value_from_none():
    assert sync.extract_category_value(None) == ""


def test_extract_category_value_from_empty_dict():
    assert sync.extract_category_value({}) == ""


# --- route ---

def test_route_defaults_to_jade_ops():
    assert sync.route("Docker infrastructure on n8n.", {}) == "jade_ops"


def test_route_detects_plants_keyword():
    assert sync.route("This is about seeds and garden care.", {}) == "plants"


def test_route_detects_coltons_plant_tracker():
    assert sync.route("Uploads go to coltons_plant_tracker.", {}) == "plants"


def test_route_detects_plant_website():
    assert sync.route("The plant_website camera flow.", {}) == "plants"


def test_route_ambiguous_raises():
    body = "plants and seeds but also docker and n8n."
    with pytest.raises(ValueError, match="Ambiguous routing"):
        sync.route(body, {})


def test_route_override_jade_ops_beats_plants_keywords():
    assert sync.route("plants seeds garden", {"baserow_target": "jade_ops"}) == "jade_ops"


def test_route_override_plants_no_keywords():
    assert sync.route("no keywords here", {"baserow_target": "plants"}) == "plants"


# --- format_log_block ---

def test_format_log_block_header():
    block = sync.format_log_block(
        ["2026-04-21T03:00:00Z|task|Fixed something"],
        "abc12345",
        "2026-04-21",
    )
    assert block.startswith("2026-04-21 | Claude Code (VPS) | session-abc12345")


def test_format_log_block_includes_task_section():
    block = sync.format_log_block(
        ["2026-04-21T03:00:00Z|task|Debugged Redis AOF"],
        "abc12345",
        "2026-04-21",
    )
    assert "Tasks:" in block
    assert "- Debugged Redis AOF" in block


def test_format_log_block_deduplicates_files():
    lines = [
        "2026-04-21T03:00:00Z|file|docker/baserow/docker-compose.yml",
        "2026-04-21T03:05:00Z|file|docker/baserow/docker-compose.yml",
    ]
    block = sync.format_log_block(lines, "abc12345", "2026-04-21")
    assert block.count("docker/baserow/docker-compose.yml") == 1


def test_format_log_block_omits_empty_sections():
    block = sync.format_log_block(
        ["2026-04-21T03:00:00Z|task|Something"],
        "abc12345",
        "2026-04-21",
    )
    assert "Bugs:" not in block
    assert "Files changed:" not in block
    assert "Decisions:" not in block


def test_format_log_block_skips_malformed_lines():
    lines = ["no-pipes-here", "2026-04-21T03:00:00Z|task|Good task"]
    block = sync.format_log_block(lines, "abc12345", "2026-04-21")
    assert "no-pipes-here" not in block
    assert "Good task" in block


def test_format_log_block_multiple_sections():
    lines = [
        "2026-04-21T03:00:00Z|task|Task one",
        "2026-04-21T03:01:00Z|bug|Bug found",
        "2026-04-21T03:02:00Z|decision|Keep AOF on",
    ]
    block = sync.format_log_block(lines, "abc12345", "2026-04-21")
    assert "Tasks:" in block
    assert "Bugs:" in block
    assert "Decisions:" in block


# --- load_manifest ---

def test_load_manifest_returns_empty_dict_when_file_missing(tmp_path):
    assert sync.load_manifest(tmp_path / "nope.json") == {}


def test_load_manifest_parses_valid_json(tmp_path):
    mf = tmp_path / ".manifest.json"
    mf.write_text(json.dumps({"k": {"table": 668, "row_id": 1, "version": 3}}))
    result = sync.load_manifest(mf)
    assert result["k"]["version"] == 3


def test_load_manifest_returns_empty_dict_on_corrupt_json(tmp_path):
    mf = tmp_path / ".manifest.json"
    mf.write_text("not json {{{")
    assert sync.load_manifest(mf) == {}


# --- build_pull_file_content ---

def test_build_pull_file_content_includes_frontmatter():
    row = {
        "key": "03-baserow-schema",
        "title": "Baserow Schema",
        "category": {"id": 2848, "value": "schema", "color": "yellow"},
        "version": 10,
        "last_updated": "2026-04-01",
        "content": "## Schema content",
        "id": 4,
    }
    content = sync.build_pull_file_content(row, 668, "jade_ops")
    assert "source: jade_ops_baserow" in content
    assert "table: 668" in content
    assert "key: 03-baserow-schema" in content
    assert "version: 10" in content
    assert "## Schema content" in content


def test_build_pull_file_content_handles_null_category():
    row = {
        "key": "test",
        "title": "Test",
        "category": None,
        "version": 1,
        "last_updated": "2026-04-21",
        "content": "",
        "id": 99,
    }
    content = sync.build_pull_file_content(row, 668, "jade_ops")
    assert "category: " in content  # empty string, no crash
```

- [ ] **Step 2: Run tests — expect failures**

```bash
cd /root/.claude/hooks/baserow-sync && python3 -m pytest tests/ -v 2>&1 | head -30
```

Expected: `ImportError` or `AttributeError` — functions not defined yet.

- [ ] **Step 3: Implement pure functions in sync.py**

Replace `sync.py` entirely:

```python
#!/usr/bin/env python3
"""Baserow <-> Claude Code memory sync.

Usage:
    sync.py pull [--dry-run]
    sync.py stop [--dry-run]
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import frontmatter
import requests

# ---------------------------------------------------------------------------
# Paths (module-level so tests can monkeypatch)
# ---------------------------------------------------------------------------

HOOKS_DIR = Path(__file__).parent
STATE_DIR = HOOKS_DIR / "state"
MEMORY_DIR = Path("/root/.claude/projects/-root/memory")
PULL_DIR = MEMORY_DIR / "baserow_pull"
MANIFEST_FILE = PULL_DIR / ".manifest.json"
SESSION_LOG_FILE = STATE_DIR / "session-log.txt"
PENDING_PUSHES_FILE = STATE_DIR / "pending-pushes.json"
LAST_STOP_ERROR_FILE = STATE_DIR / "last-stop-error.log"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLANTS_KEYWORDS = {"plants", "coltons_plant_tracker", "seeds", "garden", "plant_website"}
JADE_OPS_KEYWORDS = {"n8n", "docker", "proxmox", "pct 300", "jade-ops", "jade_ops"}

TYPE_TO_CATEGORY = {
    "project": "state",
    "feedback": "protocol",
    "reference": "schema",
    "user": "startup",
}

LOG_SECTIONS = [
    ("Tasks", "task"),
    ("Bugs", "bug"),
    ("Files changed", "file"),
    ("Decisions", "decision"),
    ("Built", "built"),
    ("Notes", "note"),
]

# ---------------------------------------------------------------------------
# Pure functions — no I/O, fully testable
# ---------------------------------------------------------------------------


def load_env(env_file: Path) -> dict:
    """Parse a simple KEY=VALUE env file."""
    env = {}
    if not env_file.exists():
        return env
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def derive_key(file_path: Path, meta: dict) -> str:
    """Return Baserow row key: frontmatter baserow_key override or filename stem."""
    return meta.get("baserow_key") or file_path.stem


def type_to_category(type_str: str) -> str:
    """Map frontmatter 'type' to a Baserow category string."""
    return TYPE_TO_CATEGORY.get(type_str, "state")


def extract_category_value(category_field) -> str:
    """Extract string value from a Baserow single_select field (dict or None)."""
    if isinstance(category_field, dict):
        return category_field.get("value", "")
    return ""


def route(body: str, meta: dict) -> str:
    """Return 'jade_ops' or 'plants'. Raises ValueError if ambiguous.

    Checks frontmatter 'baserow_target' first (explicit override).
    Otherwise uses keyword scan: plants keywords → plants, else → jade_ops.
    Ambiguous = body matches BOTH sets → caller must set baserow_target.
    """
    if "baserow_target" in meta:
        return meta["baserow_target"]
    body_lower = body.lower()
    has_plants = any(kw in body_lower for kw in PLANTS_KEYWORDS)
    has_jade = any(kw in body_lower for kw in JADE_OPS_KEYWORDS)
    if has_plants and has_jade:
        raise ValueError(
            "Ambiguous routing: body matches both jade_ops and plants keywords. "
            "Add 'baserow_target: jade_ops' or 'baserow_target: plants' to frontmatter."
        )
    return "plants" if has_plants else "jade_ops"


def format_log_block(log_lines: list, session_id: str, date_str: str) -> str:
    """Format pending session-log lines into a row-7 compatible block."""
    entries = []
    for line in log_lines:
        parts = line.split("|", 2)
        if len(parts) == 3:
            entries.append((parts[1].strip(), parts[2].strip()))

    header = f"{date_str} | Claude Code (VPS) | session-{session_id}"
    sections = []
    for label, cat in LOG_SECTIONS:
        items = [text for (c, text) in entries if c == cat]
        if cat == "file":
            items = list(dict.fromkeys(items))  # dedup, preserve order
        if items:
            bullets = "\n".join(f"- {i}" for i in items)
            sections.append(f"{label}:\n{bullets}")

    return header + "\n" + "\n".join(sections)


def load_manifest(manifest_file: Path) -> dict:
    """Load manifest JSON. Returns {} if missing or corrupt."""
    if not manifest_file.exists():
        return {}
    try:
        return json.loads(manifest_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def build_pull_file_content(row: dict, table_id: int, prefix: str) -> str:
    """Build markdown file content for a pulled Baserow row."""
    category_value = extract_category_value(row.get("category"))
    pulled_at = datetime.now(timezone.utc).isoformat()
    body = row.get("content", "") or ""
    return (
        f"---\n"
        f"source: {prefix}_baserow\n"
        f"table: {table_id}\n"
        f"key: {row['key']}\n"
        f"title: {row.get('title', '')}\n"
        f"category: {category_value}\n"
        f"version: {row.get('version', 1)}\n"
        f"last_updated: {row.get('last_updated', '')}\n"
        f"pulled_at: {pulled_at}\n"
        f"---\n\n"
        f"{body}"
    )


# ---------------------------------------------------------------------------
# Placeholder for HTTP + subcommand functions (added in later tasks)
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Baserow <-> Claude Code memory sync")
    parser.add_argument("command", choices=["pull", "stop"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    env = load_env(HOOKS_DIR / ".env")

    if args.command == "pull":
        print("pull: not yet implemented")
    elif args.command == "stop":
        print("stop: not yet implemented")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests — expect all to pass**

```bash
cd /root/.claude/hooks/baserow-sync && python3 -m pytest tests/ -v
```

Expected: all tests pass. Example output:
```
test_derive_key_uses_filename_by_default PASSED
test_derive_key_uses_frontmatter_override PASSED
...
32 passed in 0.XXs
```

- [ ] **Step 5: Commit**

```bash
git -C /root add /root/.claude/hooks/baserow-sync/sync.py /root/.claude/hooks/baserow-sync/tests/test_sync.py
git -C /root commit -m "feat: add pure functions for baserow memory sync"
```

---

## Task 3: HTTP Helpers + Manifest Operations

**Files:**
- Modify: `/root/.claude/hooks/baserow-sync/tests/test_sync.py` (append)
- Modify: `/root/.claude/hooks/baserow-sync/sync.py` (replace placeholder section)

- [ ] **Step 1: Append tests for HTTP helpers and manifest write**

Add to the end of `tests/test_sync.py`:

```python
# --- HTTP helpers (mocked) ---

from unittest.mock import MagicMock, patch


def _mock_response(data: dict):
    m = MagicMock()
    m.json.return_value = data
    m.raise_for_status.return_value = None
    return m


def test_list_rows_calls_correct_url():
    with patch("requests.get", return_value=_mock_response({"results": []})) as mock_get:
        result = sync.list_rows("https://example.com", 668, "tok")
    mock_get.assert_called_once()
    call_args = mock_get.call_args
    assert "/api/database/rows/table/668/" in call_args[0][0]
    assert call_args[1]["params"]["size"] == 200
    assert result == []


def test_get_row_calls_correct_url():
    with patch("requests.get", return_value=_mock_response({"id": 7, "key": "06-session-log"})) as mock_get:
        result = sync.get_row("https://example.com", 668, 7, "tok")
    assert "/api/database/rows/table/668/7/" in mock_get.call_args[0][0]
    assert result["key"] == "06-session-log"


def test_patch_row_sends_json():
    with patch("requests.patch", return_value=_mock_response({"id": 7})) as mock_patch:
        sync.patch_row("https://example.com", 668, 7, "tok", {"content": "new"})
    call_kwargs = mock_patch.call_args[1]
    assert call_kwargs["json"] == {"content": "new"}
    assert "Token tok" in call_kwargs["headers"]["Authorization"]


def test_create_row_posts_to_table():
    with patch("requests.post", return_value=_mock_response({"id": 99})) as mock_post:
        result = sync.create_row("https://example.com", 668, "tok", {"key": "new-key"})
    assert "/api/database/rows/table/668/" in mock_post.call_args[0][0]
    assert result["id"] == 99
```

- [ ] **Step 2: Run — expect 4 new failures**

```bash
cd /root/.claude/hooks/baserow-sync && python3 -m pytest tests/ -v -k "test_list_rows or test_get_row or test_patch_row or test_create_row"
```

Expected: 4 failures (AttributeError: module 'sync' has no attribute 'list_rows')

- [ ] **Step 3: Add HTTP helpers to sync.py**

Replace the `# Placeholder for HTTP` section in `sync.py` with:

```python
# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def make_headers(token: str) -> dict:
    return {"Authorization": f"Token {token}"}


def list_rows(base_url: str, table_id: int, token: str) -> list:
    resp = requests.get(
        f"{base_url}/api/database/rows/table/{table_id}/",
        headers=make_headers(token),
        params={"user_field_names": "true", "size": 200},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["results"]


def get_row(base_url: str, table_id: int, row_id: int, token: str) -> dict:
    resp = requests.get(
        f"{base_url}/api/database/rows/table/{table_id}/{row_id}/",
        headers=make_headers(token),
        params={"user_field_names": "true"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def patch_row(base_url: str, table_id: int, row_id: int, token: str, data: dict) -> dict:
    resp = requests.patch(
        f"{base_url}/api/database/rows/table/{table_id}/{row_id}/",
        headers={**make_headers(token), "Content-Type": "application/json"},
        params={"user_field_names": "true"},
        json=data,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def create_row(base_url: str, table_id: int, token: str, data: dict) -> dict:
    resp = requests.post(
        f"{base_url}/api/database/rows/table/{table_id}/",
        headers={**make_headers(token), "Content-Type": "application/json"},
        params={"user_field_names": "true"},
        json=data,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()
```

Keep the `main()` function at the bottom of `sync.py`.

- [ ] **Step 4: Run all tests — expect all pass**

```bash
cd /root/.claude/hooks/baserow-sync && python3 -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git -C /root add /root/.claude/hooks/baserow-sync/sync.py /root/.claude/hooks/baserow-sync/tests/test_sync.py
git -C /root commit -m "feat: add HTTP helpers for baserow REST API calls"
```

---

## Task 4: Pull Subcommand

**Files:**
- Modify: `/root/.claude/hooks/baserow-sync/tests/test_sync.py` (append)
- Modify: `/root/.claude/hooks/baserow-sync/sync.py` (add do_pull + helpers)

- [ ] **Step 1: Append pull tests**

Add to the end of `tests/test_sync.py`:

```python
# --- do_pull ---

FAKE_JADE_ROW = {
    "id": 4,
    "key": "03-baserow-schema",
    "title": "Baserow Schema",
    "category": {"id": 2848, "value": "schema", "color": "yellow"},
    "version": 10,
    "last_updated": "2026-04-01",
    "content": "## Schema content",
    "active": True,
}

FAKE_PLANTS_ROW = {
    "id": 1,
    "key": "project_plant_website",
    "title": "Plant Website",
    "category": {"id": 3731, "value": "state", "color": "green"},
    "version": 2,
    "last_updated": "2026-04-21",
    "content": "Camera flow info",
    "active": True,
}


def test_do_pull_writes_jade_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "PULL_DIR", tmp_path / "baserow_pull")
    monkeypatch.setattr(sync, "MANIFEST_FILE", tmp_path / "baserow_pull" / ".manifest.json")
    monkeypatch.setattr(sync, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(sync, "SESSION_LOG_FILE", tmp_path / "state" / "session-log.txt")
    monkeypatch.setattr(sync, "PENDING_PUSHES_FILE", tmp_path / "state" / "pending-pushes.json")

    env = {
        "BASEROW_BASE_URL": "https://example.com",
        "BASEROW_TOKEN": "tok",
        "JADE_OPS_TABLE_ID": "668",
        "PLANTS_TABLE_ID": "815",
    }

    with patch("sync.list_rows") as mock_list:
        mock_list.side_effect = [
            [FAKE_JADE_ROW],  # jade call
            [FAKE_PLANTS_ROW],  # plants call
        ]
        sync.do_pull(env)

    jade_file = tmp_path / "baserow_pull" / "jade_ops_03-baserow-schema.md"
    assert jade_file.exists()
    assert "source: jade_ops_baserow" in jade_file.read_text()
    assert "## Schema content" in jade_file.read_text()


def test_do_pull_writes_plants_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "PULL_DIR", tmp_path / "baserow_pull")
    monkeypatch.setattr(sync, "MANIFEST_FILE", tmp_path / "baserow_pull" / ".manifest.json")
    monkeypatch.setattr(sync, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(sync, "SESSION_LOG_FILE", tmp_path / "state" / "session-log.txt")
    monkeypatch.setattr(sync, "PENDING_PUSHES_FILE", tmp_path / "state" / "pending-pushes.json")

    env = {
        "BASEROW_BASE_URL": "https://example.com",
        "BASEROW_TOKEN": "tok",
        "JADE_OPS_TABLE_ID": "668",
        "PLANTS_TABLE_ID": "815",
    }

    with patch("sync.list_rows") as mock_list:
        mock_list.side_effect = [[FAKE_JADE_ROW], [FAKE_PLANTS_ROW]]
        sync.do_pull(env)

    plants_file = tmp_path / "baserow_pull" / "plants_project_plant_website.md"
    assert plants_file.exists()


def test_do_pull_writes_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "PULL_DIR", tmp_path / "baserow_pull")
    monkeypatch.setattr(sync, "MANIFEST_FILE", tmp_path / "baserow_pull" / ".manifest.json")
    monkeypatch.setattr(sync, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(sync, "SESSION_LOG_FILE", tmp_path / "state" / "session-log.txt")
    monkeypatch.setattr(sync, "PENDING_PUSHES_FILE", tmp_path / "state" / "pending-pushes.json")

    env = {
        "BASEROW_BASE_URL": "https://example.com",
        "BASEROW_TOKEN": "tok",
        "JADE_OPS_TABLE_ID": "668",
        "PLANTS_TABLE_ID": "815",
    }

    with patch("sync.list_rows") as mock_list:
        mock_list.side_effect = [[FAKE_JADE_ROW], []]
        sync.do_pull(env)

    manifest = json.loads((tmp_path / "baserow_pull" / ".manifest.json").read_text())
    assert "03-baserow-schema" in manifest
    entry = manifest["03-baserow-schema"]
    assert entry["table"] == 668
    assert entry["row_id"] == 4
    assert entry["version"] == 10


def test_do_pull_skips_inactive_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "PULL_DIR", tmp_path / "baserow_pull")
    monkeypatch.setattr(sync, "MANIFEST_FILE", tmp_path / "baserow_pull" / ".manifest.json")
    monkeypatch.setattr(sync, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(sync, "SESSION_LOG_FILE", tmp_path / "state" / "session-log.txt")
    monkeypatch.setattr(sync, "PENDING_PUSHES_FILE", tmp_path / "state" / "pending-pushes.json")

    inactive_row = {**FAKE_JADE_ROW, "active": False}
    env = {
        "BASEROW_BASE_URL": "https://example.com",
        "BASEROW_TOKEN": "tok",
        "JADE_OPS_TABLE_ID": "668",
        "PLANTS_TABLE_ID": "815",
    }

    with patch("sync.list_rows") as mock_list:
        mock_list.side_effect = [[inactive_row], []]
        sync.do_pull(env)

    assert not (tmp_path / "baserow_pull" / "jade_ops_03-baserow-schema.md").exists()


def test_do_pull_deletes_orphan_files(tmp_path, monkeypatch):
    pull_dir = tmp_path / "baserow_pull"
    pull_dir.mkdir(parents=True)
    orphan = pull_dir / "jade_ops_old-deleted-row.md"
    orphan.write_text("stale content")

    monkeypatch.setattr(sync, "PULL_DIR", pull_dir)
    monkeypatch.setattr(sync, "MANIFEST_FILE", pull_dir / ".manifest.json")
    monkeypatch.setattr(sync, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(sync, "SESSION_LOG_FILE", tmp_path / "state" / "session-log.txt")
    monkeypatch.setattr(sync, "PENDING_PUSHES_FILE", tmp_path / "state" / "pending-pushes.json")

    env = {
        "BASEROW_BASE_URL": "https://example.com",
        "BASEROW_TOKEN": "tok",
        "JADE_OPS_TABLE_ID": "668",
        "PLANTS_TABLE_ID": "815",
    }

    with patch("sync.list_rows") as mock_list:
        mock_list.side_effect = [[], []]
        sync.do_pull(env)

    assert not orphan.exists()


def test_do_pull_dry_run_writes_no_files(tmp_path, monkeypatch):
    pull_dir = tmp_path / "baserow_pull"
    monkeypatch.setattr(sync, "PULL_DIR", pull_dir)
    monkeypatch.setattr(sync, "MANIFEST_FILE", pull_dir / ".manifest.json")
    monkeypatch.setattr(sync, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(sync, "SESSION_LOG_FILE", tmp_path / "state" / "session-log.txt")
    monkeypatch.setattr(sync, "PENDING_PUSHES_FILE", tmp_path / "state" / "pending-pushes.json")

    env = {
        "BASEROW_BASE_URL": "https://example.com",
        "BASEROW_TOKEN": "tok",
        "JADE_OPS_TABLE_ID": "668",
        "PLANTS_TABLE_ID": "815",
    }

    with patch("sync.list_rows") as mock_list:
        mock_list.side_effect = [[FAKE_JADE_ROW], []]
        sync.do_pull(env, dry_run=True)

    assert not pull_dir.exists()
```

- [ ] **Step 2: Run — expect new failures**

```bash
cd /root/.claude/hooks/baserow-sync && python3 -m pytest tests/ -v -k "test_do_pull"
```

Expected: `AttributeError: module 'sync' has no attribute 'do_pull'`

- [ ] **Step 3: Add do_pull and recovery helpers to sync.py**

Insert before the `main()` function in `sync.py`:

```python
# ---------------------------------------------------------------------------
# Pull subcommand
# ---------------------------------------------------------------------------


def do_pull(env: dict, dry_run: bool = False) -> None:
    base_url = env["BASEROW_BASE_URL"]
    token = env["BASEROW_TOKEN"]
    jade_table = int(env["JADE_OPS_TABLE_ID"])
    plants_table = int(env["PLANTS_TABLE_ID"])

    if not dry_run:
        PULL_DIR.mkdir(parents=True, exist_ok=True)
        STATE_DIR.mkdir(parents=True, exist_ok=True)

    manifest = {}
    existing_files = {f.name for f in PULL_DIR.glob("*.md")} if PULL_DIR.exists() else set()
    seen_files = set()
    jade_count = 0
    plants_count = 0

    for table_id, prefix in [(jade_table, "jade_ops"), (plants_table, "plants")]:
        rows = list_rows(base_url, table_id, token)
        for row in rows:
            if not row.get("active"):
                continue
            key = row["key"]
            filename = f"{prefix}_{key}.md"
            seen_files.add(filename)

            if not dry_run:
                (PULL_DIR / filename).write_text(build_pull_file_content(row, table_id, prefix))

            manifest[key] = {
                "file": filename,
                "table": table_id,
                "row_id": row["id"],
                "version": row.get("version", 1),
                "last_updated": row.get("last_updated", ""),
                "prefix": prefix,
            }
            if table_id == jade_table:
                jade_count += 1
            else:
                plants_count += 1

    # Delete orphan files (rows removed/deactivated in Baserow)
    for filename in existing_files - seen_files:
        if not filename.startswith(".") and not dry_run:
            (PULL_DIR / filename).unlink(missing_ok=True)

    if not dry_run:
        MANIFEST_FILE.write_text(json.dumps(manifest, indent=2))

    # Flush any leftover log from a crashed prior session
    _flush_recovered_log(env, dry_run)

    # Retry any pushes that failed during a prior session
    _retry_pending_pushes(env, dry_run)

    print(
        f"Baserow pulled: {jade_count} rows from {jade_table}, "
        f"{plants_count} from {plants_table}. "
        f"See {PULL_DIR} for canonical context."
    )


def _flush_recovered_log(env: dict, dry_run: bool) -> None:
    if SESSION_LOG_FILE.exists() and SESSION_LOG_FILE.read_text().strip():
        print("[recovery] Leftover session-log.txt found — flushing from crashed prior session.")
        try:
            _push_session_log(env, dry_run, recovered=True)
        except Exception as exc:
            print(f"[recovery] Flush failed: {exc}", file=sys.stderr)


def _retry_pending_pushes(env: dict, dry_run: bool) -> None:
    if not PENDING_PUSHES_FILE.exists():
        return
    try:
        pending = json.loads(PENDING_PUSHES_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return
    if not pending:
        return
    remaining = []
    for item in pending:
        try:
            _execute_pending_push(env, item, dry_run)
        except Exception as exc:
            print(f"[pending] Retry failed for {item.get('key')}: {exc}", file=sys.stderr)
            remaining.append(item)
    if not dry_run:
        if remaining:
            PENDING_PUSHES_FILE.write_text(json.dumps(remaining, indent=2))
        else:
            PENDING_PUSHES_FILE.unlink(missing_ok=True)


def _execute_pending_push(env: dict, item: dict, dry_run: bool) -> None:
    """Execute a single queued push from pending-pushes.json."""
    base_url = env["BASEROW_BASE_URL"]
    token = env["BASEROW_TOKEN"]
    if not dry_run:
        if item.get("row_id"):
            patch_row(base_url, item["table_id"], item["row_id"], token, item["data"])
        else:
            create_row(base_url, item["table_id"], token, item["data"])
```

- [ ] **Step 4: Run all tests — expect all pass**

```bash
cd /root/.claude/hooks/baserow-sync && python3 -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git -C /root add /root/.claude/hooks/baserow-sync/sync.py /root/.claude/hooks/baserow-sync/tests/test_sync.py
git -C /root commit -m "feat: implement pull subcommand with orphan cleanup and recovery"
```

---

## Task 5: Stop Subcommand

**Files:**
- Modify: `/root/.claude/hooks/baserow-sync/tests/test_sync.py` (append)
- Modify: `/root/.claude/hooks/baserow-sync/sync.py` (add do_stop + _push_session_log)

- [ ] **Step 1: Append stop tests**

Add to the end of `tests/test_sync.py`:

```python
# --- do_stop ---

FAKE_SESSION_LOG_ROW = {
    "id": 7,
    "key": "06-session-log",
    "version": 33,
    "last_updated": "2026-04-11",
    "content": "Previous session content here.",
}


def test_do_stop_does_nothing_when_log_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "SESSION_LOG_FILE", tmp_path / "session-log.txt")
    # File doesn't exist → should exit cleanly
    env = {
        "BASEROW_BASE_URL": "https://example.com",
        "BASEROW_TOKEN": "tok",
        "JADE_OPS_TABLE_ID": "668",
        "SESSION_LOG_ROW_ID": "7",
    }
    with patch("sync.get_row") as mock_get, patch("sync.patch_row") as mock_patch:
        sync.do_stop(env)
    mock_get.assert_not_called()
    mock_patch.assert_not_called()


def test_do_stop_prepends_block_to_row7(tmp_path, monkeypatch):
    log_file = tmp_path / "session-log.txt"
    log_file.write_text("2026-04-21T03:00:00Z|task|Fixed Redis AOF\n")
    monkeypatch.setattr(sync, "SESSION_LOG_FILE", log_file)
    monkeypatch.setattr(sync, "LAST_STOP_ERROR_FILE", tmp_path / "last-stop-error.log")

    env = {
        "BASEROW_BASE_URL": "https://example.com",
        "BASEROW_TOKEN": "tok",
        "JADE_OPS_TABLE_ID": "668",
        "SESSION_LOG_ROW_ID": "7",
    }

    with (
        patch("sync.get_row", return_value=FAKE_SESSION_LOG_ROW),
        patch("sync.patch_row") as mock_patch,
        patch.dict(os.environ, {"CLAUDE_SESSION_ID": "abc12345xyz"}),
    ):
        sync.do_stop(env)

    assert mock_patch.called
    patch_data = mock_patch.call_args[0][4]  # positional arg 'data'
    assert "Claude Code (VPS)" in patch_data["content"]
    assert "Fixed Redis AOF" in patch_data["content"]
    assert "Previous session content here." in patch_data["content"]
    assert patch_data["version"] == 34  # was 33, now 34


def test_do_stop_truncates_log_after_push(tmp_path, monkeypatch):
    log_file = tmp_path / "session-log.txt"
    log_file.write_text("2026-04-21T03:00:00Z|task|Something\n")
    monkeypatch.setattr(sync, "SESSION_LOG_FILE", log_file)
    monkeypatch.setattr(sync, "LAST_STOP_ERROR_FILE", tmp_path / "last-stop-error.log")

    env = {
        "BASEROW_BASE_URL": "https://example.com",
        "BASEROW_TOKEN": "tok",
        "JADE_OPS_TABLE_ID": "668",
        "SESSION_LOG_ROW_ID": "7",
    }

    with (
        patch("sync.get_row", return_value=FAKE_SESSION_LOG_ROW),
        patch("sync.patch_row"),
        patch.dict(os.environ, {"CLAUDE_SESSION_ID": "abc12345xyz"}),
    ):
        sync.do_stop(env)

    assert not log_file.exists()


def test_do_stop_dry_run_does_not_patch(tmp_path, monkeypatch):
    log_file = tmp_path / "session-log.txt"
    log_file.write_text("2026-04-21T03:00:00Z|task|Dry run task\n")
    monkeypatch.setattr(sync, "SESSION_LOG_FILE", log_file)
    monkeypatch.setattr(sync, "LAST_STOP_ERROR_FILE", tmp_path / "last-stop-error.log")

    env = {
        "BASEROW_BASE_URL": "https://example.com",
        "BASEROW_TOKEN": "tok",
        "JADE_OPS_TABLE_ID": "668",
        "SESSION_LOG_ROW_ID": "7",
    }

    with (
        patch("sync.get_row", return_value=FAKE_SESSION_LOG_ROW),
        patch("sync.patch_row") as mock_patch,
        patch.dict(os.environ, {"CLAUDE_SESSION_ID": "abc12345xyz"}),
    ):
        sync.do_stop(env, dry_run=True)

    mock_patch.assert_not_called()
    assert log_file.exists()  # not deleted in dry-run


def test_do_stop_writes_error_log_on_failure(tmp_path, monkeypatch):
    log_file = tmp_path / "session-log.txt"
    log_file.write_text("2026-04-21T03:00:00Z|task|Something\n")
    error_log = tmp_path / "last-stop-error.log"
    monkeypatch.setattr(sync, "SESSION_LOG_FILE", log_file)
    monkeypatch.setattr(sync, "LAST_STOP_ERROR_FILE", error_log)

    env = {
        "BASEROW_BASE_URL": "https://example.com",
        "BASEROW_TOKEN": "tok",
        "JADE_OPS_TABLE_ID": "668",
        "SESSION_LOG_ROW_ID": "7",
    }

    with patch("sync.get_row", side_effect=Exception("Network error")):
        sync.do_stop(env)  # should not raise

    assert error_log.exists()
    assert log_file.exists()  # preserved so next session can recover
```

Also add `import os` to the top of `tests/test_sync.py` (just below `import json`).

- [ ] **Step 2: Run — expect new failures**

```bash
cd /root/.claude/hooks/baserow-sync && python3 -m pytest tests/ -v -k "test_do_stop"
```

Expected: `AttributeError: module 'sync' has no attribute 'do_stop'`

- [ ] **Step 3: Add do_stop and _push_session_log to sync.py**

Insert before `main()` in `sync.py`:

```python
# ---------------------------------------------------------------------------
# Stop subcommand
# ---------------------------------------------------------------------------


def do_stop(env: dict, dry_run: bool = False) -> None:
    if not SESSION_LOG_FILE.exists() or not SESSION_LOG_FILE.read_text().strip():
        return
    try:
        _push_session_log(env, dry_run)
    except Exception as exc:
        error_msg = f"{datetime.now(timezone.utc).isoformat()}: {exc}"
        if not dry_run:
            LAST_STOP_ERROR_FILE.write_text(error_msg)
        print(f"[stop] Failed to push session log: {exc}", file=sys.stderr)
        sys.exit(0)  # Never block session stop


def _push_session_log(env: dict, dry_run: bool, recovered: bool = False) -> None:
    log_content = SESSION_LOG_FILE.read_text().strip()
    if not log_content:
        return

    log_lines = log_content.splitlines()
    session_id = os.environ.get("CLAUDE_SESSION_ID", "unknown")[:8]
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    block = format_log_block(log_lines, session_id, date_str)
    if recovered:
        block = "[recovered from incomplete prior session]\n" + block

    base_url = env["BASEROW_BASE_URL"]
    token = env["BASEROW_TOKEN"]
    jade_table = int(env["JADE_OPS_TABLE_ID"])
    row_id = int(env["SESSION_LOG_ROW_ID"])

    current_row = get_row(base_url, jade_table, row_id, token)
    current_version = current_row.get("version", 1)
    current_content = current_row.get("content", "")

    new_content = block + "\n\n---\n\n" + current_content

    if not dry_run:
        patch_row(
            base_url,
            jade_table,
            row_id,
            token,
            {
                "content": new_content,
                "version": current_version + 1,
                "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            },
        )
        SESSION_LOG_FILE.unlink()
    else:
        print("[dry-run] Would prepend to session log:")
        print(block)
```

Also update `main()` to wire the subcommands:

```python
def main():
    parser = argparse.ArgumentParser(description="Baserow <-> Claude Code memory sync")
    parser.add_argument("command", choices=["pull", "stop"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    env = load_env(HOOKS_DIR / ".env")

    if args.command == "pull":
        do_pull(env, dry_run=args.dry_run)
    elif args.command == "stop":
        do_stop(env, dry_run=args.dry_run)
```

- [ ] **Step 4: Run all tests — expect all pass**

```bash
cd /root/.claude/hooks/baserow-sync && python3 -m pytest tests/ -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git -C /root add /root/.claude/hooks/baserow-sync/sync.py /root/.claude/hooks/baserow-sync/tests/test_sync.py
git -C /root commit -m "feat: implement stop subcommand with session-log push and error recovery"
```

---

## Task 6: Hook Wrappers + settings.json Registration

**Files:**
- Create: `/root/.claude/hooks/baserow-sync/pull.sh`
- Create: `/root/.claude/hooks/baserow-sync/stop.sh`
- Modify: `/root/.claude/settings.json`

- [ ] **Step 1: Create pull.sh**

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 sync.py pull
```

```bash
chmod +x /root/.claude/hooks/baserow-sync/pull.sh
```

- [ ] **Step 2: Create stop.sh**

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 sync.py stop
```

```bash
chmod +x /root/.claude/hooks/baserow-sync/stop.sh
```

- [ ] **Step 3: Verify pull.sh runs against live Baserow (dry-run first)**

Fill in the actual Baserow token in `.env` now if not done yet (from n8n workflow `yPaq1M8Bxcuxh3p7` HTTP Request node). Then:

```bash
/root/.claude/hooks/baserow-sync/pull.sh --dry-run 2>&1 || python3 /root/.claude/hooks/baserow-sync/sync.py pull --dry-run
```

Wait — `pull.sh` doesn't pass args through. Test the Python script directly:

```bash
python3 /root/.claude/hooks/baserow-sync/sync.py pull --dry-run
```

Expected output: `Baserow pulled: 12 rows from 668, 3 from 815. See /root/.claude/projects/-root/memory/baserow_pull for canonical context.`

If you see `401` or connection errors: check `.env` for the correct token and `BASEROW_BASE_URL`.

- [ ] **Step 4: Register hooks in settings.json**

Current content of `/root/.claude/settings.json`:
```json
{
  "enabledPlugins": {
    "frontend-design@claude-plugins-official": true,
    "context7@claude-plugins-official": true,
    "superpowers@claude-plugins-official": true
  },
  "model": "sonnet"
}
```

Replace with:
```json
{
  "enabledPlugins": {
    "frontend-design@claude-plugins-official": true,
    "context7@claude-plugins-official": true,
    "superpowers@claude-plugins-official": true
  },
  "model": "sonnet",
  "hooks": {
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "/root/.claude/hooks/baserow-sync/pull.sh"
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "/root/.claude/hooks/baserow-sync/stop.sh"
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 5: Commit**

```bash
git -C /root add /root/.claude/hooks/baserow-sync/pull.sh /root/.claude/hooks/baserow-sync/stop.sh /root/.claude/settings.json
git -C /root commit -m "feat: register SessionStart and Stop hooks for baserow sync"
```

---

## Task 7: Update MEMORY.md + Expand user_profile.md

**Files:**
- Modify: `/root/.claude/projects/-root/memory/MEMORY.md`
- Modify: `/root/.claude/projects/-root/memory/user_profile.md`

- [ ] **Step 1: Add baserow_pull pointer to MEMORY.md**

Add these two lines at the top of `MEMORY.md`:

```markdown
- [Baserow ops context](baserow_pull/) — pulled at session start; canonical jade-ops + plants state. Read files in this dir for authoritative Baserow content.
- [In-session push protocol](baserow_pull/) — when saving/updating any project_*.md, feedback_*.md, reference_*.md, or user_*.md file: immediately call Baserow MCP to upsert the corresponding row (version-check first via .manifest.json). Route jade-ops files → table 668, plants files → table 815.
```

So `MEMORY.md` becomes:

```markdown
- [Baserow ops context](baserow_pull/) — pulled at session start; canonical jade-ops + plants state. Read files in this dir for authoritative Baserow content.
- [In-session push protocol](baserow_pull/) — when saving/updating any project_*.md, feedback_*.md, reference_*.md, or user_*.md file: immediately call Baserow MCP to upsert the corresponding row (version-check first via .manifest.json). Route jade-ops files → table 668, plants files → table 815.
- [User profile](user_profile.md) — Jade Properties Group infra admin, Proxmox/Docker, values reliability
- [Baserow stack](project_baserow_stack.md) — Docker architecture, hardening done 2026-04-11, Redis AOF fix 2026-04-13
- [Backup retention](project_backup_retention.md) — Recursive snowball incident, retention policy, preventive measures in backup.sh
- [Baserow MCP](reference_baserow_mcp.md) — MCP server "Coltons Plants Baserow" for table operations
- [Baserow FD leak](project_baserow_fd_leak.md) — Recurring upload 500s = Errno 24 (FD exhaustion) in baserow container, not Postgres/Redis
- [Plant website project](project_plant_website.md) — Separate Claude-managed project; camera flow uploads to Coltons Plants Baserow
- [Shell-Python injection](feedback_upgrade_script.md) — Never interpolate shell vars into Python literals, use env vars
```

- [ ] **Step 2: Expand user_profile.md to superset of row 8**

Replace the contents of `user_profile.md` entirely. Row 8 in Baserow (`07-bradly-preferences`) contains the authoritative content. The local file must be a complete superset before the first push overwrites it.

```markdown
---
name: User profile
description: Bradly's identity, communication style, preferences, and testing protocol
type: user
baserow_key: 07-bradly-preferences
---

# Bradly Preferences — Working Style & Communication
# Last updated: 2026-04-21
# BEHAVIORAL RULES (auto-logging, testing, wrap-up) are in CLAUDE.md on Windows.
# This file contains personal preferences, communication style, and infra context.

## Identity
- Always address as Bradly (never Brad, never Mr. Goodner)
- Partner: Colton Heller (co-operator, not always present in chats)

## Infra Context (Claude Code / VPS)
- Runs PropertyOps infrastructure for Jade Properties Group on Proxmox VM (pct 300)
- Manages Docker-based services: Baserow, n8n, DocuSeal
- Baserow is the critical system — used for property management with image-heavy tables
- Comfortable with Docker, shell scripts, and system administration
- Uses Windows desktop (Chrome) to access services
- Prefers comprehensive hardening over quick fixes

## Communication Style
- Step by step instructions with NO skipped steps
- Be explicit — never assume Bradly knows an implied step
- When building something complex, plan first, confirm the plan, then execute
- Keep responses focused and actionable — no fluff
- Flag problems immediately, never bury them at the end of a response

## Preferred Interfaces
- Claude Desktop and Claude Code are the favorites
- Claude.ai also used regularly
- Biggest pain point: no cross-interface context — table 668 + CLAUDE.md are the solution

## What Annoys Bradly (Never Do These)
- Having to repeat instructions already given in a previous session
- Being asked to say 'update the docs' or 'mark that complete' — Claude does it automatically
- Skipped steps in instructions
- Silent failures — always flag errors explicitly with the manual fix
- Jumping into building without a plan on complex tasks
- Marking a task Complete without testing it first
- Assuming something works without verifying

## Testing Checklist (quick reference — full protocol in CLAUDE.md)
- Does the happy path complete end-to-end without errors?
- Do guard conditions (idempotency checks, null guards, filters) actually work?
- Does the Baserow record get updated correctly?
- Does the email/PDF arrive at bradly@jadepropertiesgroup.com as expected?
- Does the Pushover alert fire?
- Do error paths exit cleanly without crashing?
- Are test fixtures cleaned up after the test?
```

- [ ] **Step 3: Commit**

```bash
git -C /root add /root/.claude/projects/-root/memory/MEMORY.md /root/.claude/projects/-root/memory/user_profile.md
git -C /root commit -m "feat: update MEMORY.md with baserow_pull protocol and expand user_profile"
```

---

## Task 8: Live Dry-Run Verification

No code changes. Verify the full flow against the real Baserow before enabling hooks.

- [ ] **Step 1: Confirm token is set in .env**

```bash
grep "BASEROW_TOKEN" /root/.claude/hooks/baserow-sync/.env
```

Expected: `BASEROW_TOKEN=<non-empty-value>` (not the placeholder text)

- [ ] **Step 2: Run pull in dry-run mode**

```bash
python3 /root/.claude/hooks/baserow-sync/sync.py pull --dry-run
```

Expected output:
```
Baserow pulled: 10 rows from 668, N rows from 815. See /root/.claude/projects/-root/memory/baserow_pull for canonical context.
```

No files should be created yet (dry-run).

- [ ] **Step 3: Run pull for real**

```bash
python3 /root/.claude/hooks/baserow-sync/sync.py pull
```

Expected: same output. Then verify:

```bash
ls /root/.claude/projects/-root/memory/baserow_pull/
```

Expected: files like `jade_ops_00-session-startup.md`, `jade_ops_03-baserow-schema.md`, `jade_ops_06-session-log.md`, `plants_project_plant_website.md`, etc. Plus `.manifest.json`.

```bash
cat /root/.claude/projects/-root/memory/baserow_pull/.manifest.json | python3 -m json.tool | head -30
```

Expected: JSON object with keys like `00-session-startup`, `03-baserow-schema`, etc., each containing `table`, `row_id`, `version`.

- [ ] **Step 4: Write a test session-log entry and dry-run stop**

```bash
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)|task|Integration test — verifying stop subcommand dry-run" >> /root/.claude/hooks/baserow-sync/state/session-log.txt
```

```bash
python3 /root/.claude/hooks/baserow-sync/sync.py stop --dry-run
```

Expected output:
```
[dry-run] Would prepend to session log:
2026-04-21 | Claude Code (VPS) | session-unknown
Tasks:
- Integration test — verifying stop subcommand dry-run
```

- [ ] **Step 5: Clean up the test log entry**

```bash
truncate -s 0 /root/.claude/hooks/baserow-sync/state/session-log.txt
```

- [ ] **Step 6: Run the full test suite one final time**

```bash
cd /root/.claude/hooks/baserow-sync && python3 -m pytest tests/ -v
```

Expected: all tests pass.

---

## Task 9: Go-Live

Enable hooks and verify end-to-end in a real session.

- [ ] **Step 1: Verify settings.json hooks are correct**

```bash
python3 -c "import json; d=json.load(open('/root/.claude/settings.json')); print(json.dumps(d['hooks'], indent=2))"
```

Expected: shows `SessionStart` and `Stop` hooks pointing to the correct `.sh` paths.

- [ ] **Step 2: Start a new Claude Code session**

Exit and restart Claude Code. At session start, the pull hook should fire automatically. You should see in the session transcript:

```
Baserow pulled: 10 rows from 668, N rows from 815. See /root/.claude/projects/-root/memory/baserow_pull for canonical context.
```

- [ ] **Step 3: Verify baserow_pull files are fresh**

In the new session, run:

```bash
ls -la /root/.claude/projects/-root/memory/baserow_pull/
```

Expected: files with today's timestamps. The `pulled_at` field in each file's frontmatter should match the current session start time.

- [ ] **Step 4: Make a trivial memory edit and verify push lands in Baserow**

Edit `project_baserow_stack.md` to bump the `last_updated` in its body. Then (as Claude, via MCP):

1. Read `baserow_pull/.manifest.json` to get `project_baserow_stack`'s row_id and version.
2. Call Jade Ops Baserow MCP `update_rows` with the new content and version+1.
3. Open `https://app.jadepropertiesgroup.com` in a browser and verify the row updated.

- [ ] **Step 5: End the session and verify session-log push**

Exit Claude Code. The Stop hook fires. Then in a browser:

Open Baserow → jade-properties-group db → claude-context table → row 7 (`06-session-log`).

Expected: a new block at the top of the content field:
```
2026-04-21 | Claude Code (VPS) | session-<id>
Tasks:
- ...
```

If row 7 shows no new block: check `state/last-stop-error.log` for the error message.

- [ ] **Step 6: Final commit tagging go-live**

```bash
git -C /root add -A
git -C /root commit -m "feat: baserow memory sync live — hooks enabled, E2E verified"
```

---

## Self-Review Notes

- **Spec §Pull → awareness mechanism (option C):** Covered — both `MEMORY.md` entry (Task 7 Step 1) and stdout from `pull.sh` (Task 6 Step 3).
- **Spec §user_profile.md superset:** Covered — Task 7 Step 2 provides the full expanded content.
- **Spec §pending-pushes.json retry:** Covered — `_retry_pending_pushes` in `do_pull`, `_execute_pending_push` helper.
- **Spec §Stop hook: last-stop-error.log:** Covered — `do_stop` writes it on failure, test `test_do_stop_writes_error_log_on_failure` verifies.
- **Spec §in-session push via MCP:** Not scripted — this is Claude's behavioral protocol, enforced by the MEMORY.md entry added in Task 7 Step 1. No code needed.
- **Spec §`--dry-run` flag:** Implemented in both `do_pull` and `do_stop`, tested for both.
- **Type → category mapping for all 4 types:** `test_type_to_category_*` covers all four.
