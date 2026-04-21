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
    """Return 'jade_ops' or 'plants'. Raises ValueError if ambiguous or invalid override.

    Checks frontmatter 'baserow_target' first (explicit override).
    Otherwise uses keyword scan: plants keywords -> plants, else -> jade_ops.
    Ambiguous = body matches BOTH sets -> caller must set baserow_target.
    """
    if "baserow_target" in meta:
        target = meta["baserow_target"]
        if target not in ("jade_ops", "plants"):
            raise ValueError(
                f"Invalid baserow_target '{target}'. Must be 'jade_ops' or 'plants'."
            )
        return target
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
    base_url = env["BASEROW_BASE_URL"]
    token = env["BASEROW_TOKEN"]
    if not dry_run:
        if item.get("row_id"):
            patch_row(base_url, item["table_id"], item["row_id"], token, item["data"])
        else:
            create_row(base_url, item["table_id"], token, item["data"])


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


if __name__ == "__main__":
    main()
