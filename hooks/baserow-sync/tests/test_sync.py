import json
import os
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


def test_route_raises_on_invalid_override():
    with pytest.raises(ValueError, match="Invalid baserow_target"):
        sync.route("anything", {"baserow_target": "jade_op"})  # typo


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


def test_build_pull_file_content_raises_on_missing_key():
    row = {"title": "No key field", "category": None, "version": 1,
           "last_updated": "2026-04-21", "content": "", "id": 99}
    with pytest.raises(KeyError):
        sync.build_pull_file_content(row, 668, "jade_ops")


# --- load_env ---

def test_load_env_parses_key_value_pairs(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("BASEROW_TOKEN=abc123\nJADE_OPS_TABLE_ID=668\n")
    result = sync.load_env(env_file)
    assert result["BASEROW_TOKEN"] == "abc123"
    assert result["JADE_OPS_TABLE_ID"] == "668"


def test_load_env_skips_comments(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# this is a comment\nBASEROW_TOKEN=tok\n")
    result = sync.load_env(env_file)
    assert "# this is a comment" not in result
    assert result["BASEROW_TOKEN"] == "tok"


def test_load_env_returns_empty_dict_when_file_missing(tmp_path):
    result = sync.load_env(tmp_path / "nonexistent.env")
    assert result == {}


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
