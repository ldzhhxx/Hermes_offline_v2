"""Tests for the WebUI MinIO sync bridge (api/minio_sync.py)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import pytest

from api import minio_sync as bridge


@pytest.fixture(autouse=True)
def _reset_state():
    """Each test starts from a clean lane state."""
    with bridge._lock:
        for lane in bridge._LANES:
            bridge._running[lane] = False
            bridge._last_result[lane] = None
    yield
    with bridge._lock:
        for lane in bridge._LANES:
            bridge._running[lane] = False
            bridge._last_result[lane] = None


def test_status_contains_safe_fields_only(monkeypatch):
    monkeypatch.setenv("HERMES_MINIO_ENABLED", "true")
    monkeypatch.setenv("HERMES_MINIO_ENDPOINT", "minio.example:9000")
    monkeypatch.setenv("HERMES_MINIO_BUCKET", "hermes-state")
    monkeypatch.setenv("HERMES_MINIO_PREFIX", "user-x/diagent")
    monkeypatch.setenv("HERMES_MINIO_ACCESS_KEY", "AKIA-SECRET")
    monkeypatch.setenv("HERMES_MINIO_SECRET_KEY", "VERY-SECRET")
    monkeypatch.setenv("HERMES_MINIO_SYNC_INTERVAL", "120")
    monkeypatch.setenv("HERMES_MINIO_SECURE", "false")

    status = bridge.get_status()
    cfg = status["config"]
    assert cfg["enabled"] is True
    assert cfg["endpoint"] == "minio.example:9000"
    assert cfg["bucket"] == "hermes-state"
    assert cfg["prefix"] == "user-x/diagent"
    assert cfg["sync_interval_seconds"] == 120
    # Sensitive keys must never appear in the public status payload.
    serialized = repr(status)
    assert "AKIA-SECRET" not in serialized
    assert "VERY-SECRET" not in serialized
    assert "access_key" not in serialized.lower()
    assert "secret_key" not in serialized.lower()


def test_trigger_state_sync_refused_when_disabled(monkeypatch):
    monkeypatch.delenv("HERMES_MINIO_ENABLED", raising=False)
    res = bridge.trigger_state_sync()
    assert res["ok"] is False
    assert "未启用" in res["error"]


def test_trigger_workspace_validates_options(monkeypatch):
    # Pretend MinIO is configured so we get past the env gate.
    monkeypatch.setenv("HERMES_MINIO_ENABLED", "true")
    monkeypatch.setenv("HERMES_MINIO_ENDPOINT", "minio:9000")
    monkeypatch.setenv("HERMES_MINIO_BUCKET", "b")
    res = bridge.trigger_workspace_sync(mode="bogus")
    assert res["ok"] is False
    assert "mode" in res["error"]
    res = bridge.trigger_workspace_sync(mode="safe", cleanup_remote=True)
    assert res["ok"] is False
    assert "cleanup" in res["error"].lower()


def test_run_subprocess_parses_result_json(monkeypatch, tmp_path):
    fake_script = tmp_path / "fake_minio_sync.py"
    fake_script.write_text(
        "import sys\n"
        "print('chatter')\n"
        "print('RESULT_JSON {\"mode\":\"state\",\"uploaded\":3,\"errors\":[]}')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bridge, "_find_minio_sync_script", lambda: fake_script)
    res = bridge._run_subprocess(["sync-state"], timeout=10)
    assert res["ok"] is True
    assert res["exit_code"] == 0
    assert res["details"]["uploaded"] == 3


def test_run_subprocess_captures_nonzero_exit(monkeypatch, tmp_path):
    fake_script = tmp_path / "fake_minio_sync.py"
    fake_script.write_text(
        "import sys\n"
        "print('something failed', file=sys.stderr)\n"
        "sys.exit(7)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bridge, "_find_minio_sync_script", lambda: fake_script)
    res = bridge._run_subprocess(["sync-state"], timeout=10)
    assert res["ok"] is False
    assert res["exit_code"] == 7
    assert "something failed" in res["error"]


def test_lane_concurrency_lock(monkeypatch):
    """Second invocation while a lane is still running must be refused."""
    monkeypatch.setenv("HERMES_MINIO_ENABLED", "true")
    monkeypatch.setenv("HERMES_MINIO_ENDPOINT", "minio:9000")
    monkeypatch.setenv("HERMES_MINIO_BUCKET", "b")

    # Manually flip the running flag so we don't have to wait for a real run.
    with bridge._lock:
        bridge._running["state"] = True
    try:
        res = bridge.trigger_state_sync()
        assert res["ok"] is False
        assert "already running" in res["error"] or "正在进行中" in res["error"]
    finally:
        with bridge._lock:
            bridge._running["state"] = False


def test_record_result_attaches_finished_at():
    bridge._record_result("state", {"ok": True})
    snap = bridge.get_status()
    last = snap["last_result"]["state"]
    assert last is not None
    assert "finished_at" in last
    assert abs(time.time() - last["finished_at"]) < 5


# ── New behavior: status payload extras + path validation ──────────────────


def test_status_unavailable_when_not_enabled(monkeypatch):
    """When MinIO is not enabled, public status surfaces a clear reason."""
    monkeypatch.delenv("HERMES_MINIO_ENABLED", raising=False)
    monkeypatch.delenv("HERMES_MINIO_REGISTER_URL", raising=False)
    status = bridge.get_status()
    assert status["configured"] is False
    assert "未启用" in (status["unavailable_reason"] or "")
    assert status["register_url"] == ""


def test_status_includes_register_url(monkeypatch):
    monkeypatch.setenv("HERMES_MINIO_REGISTER_URL", "https://example.invalid/register")
    status = bridge.get_status()
    assert status["register_url"] == "https://example.invalid/register"


def test_usage_includes_quota_total_used_remaining(monkeypatch):
    """Quota/usage fields are now on get_usage() (click-to-request), not get_status()."""
    monkeypatch.setenv("HERMES_MINIO_ENABLED", "true")
    monkeypatch.setenv("HERMES_MINIO_ENDPOINT", "minio.example:9000")
    monkeypatch.setenv("HERMES_MINIO_BUCKET", "hermes-state")
    monkeypatch.setenv("HERMES_MINIO_QUOTA_BYTES", str(10 * 1024 * 1024 * 1024))  # 10 GiB
    # Stub the metadata module so we don't try to talk to a real MinIO.
    fake_module = type("Fake", (), {})()
    fake_module.list_workspace_entries = lambda: []
    fake_module.compute_prefix_used_bytes = lambda: 2 * 1024 * 1024 * 1024  # 2 GiB
    fake_module.validate_workspace_paths = lambda paths: list(paths or [])
    monkeypatch.setattr(bridge, "_load_minio_sync_module", lambda: fake_module)

    usage = bridge.get_usage()
    assert usage["ok"] is True
    assert usage["quota_bytes"] == 10 * 1024 * 1024 * 1024
    assert usage["used_bytes"] == 2 * 1024 * 1024 * 1024
    assert usage["remaining_bytes"] == 8 * 1024 * 1024 * 1024

    # Verify get_status() no longer includes these expensive fields
    status = bridge.get_status()
    assert status["configured"] is True
    assert "quota_bytes" not in status
    assert "used_bytes" not in status
    assert "remaining_bytes" not in status


def test_usage_quota_unset_returns_none_remaining(monkeypatch):
    monkeypatch.setenv("HERMES_MINIO_ENABLED", "true")
    monkeypatch.setenv("HERMES_MINIO_ENDPOINT", "minio.example:9000")
    monkeypatch.setenv("HERMES_MINIO_BUCKET", "b")
    monkeypatch.delenv("HERMES_MINIO_QUOTA_BYTES", raising=False)
    fake_module = type("Fake", (), {})()
    fake_module.list_workspace_entries = lambda: []
    fake_module.compute_prefix_used_bytes = lambda: 0
    fake_module.validate_workspace_paths = lambda paths: list(paths or [])
    monkeypatch.setattr(bridge, "_load_minio_sync_module", lambda: fake_module)
    usage = bridge.get_usage()
    assert usage["quota_bytes"] == 0
    # Unset quota → "unknown" remaining, surfaced as None for the UI to render
    # as "—" instead of an inflated number.
    assert usage["remaining_bytes"] is None


def test_status_lists_workspace_entries(monkeypatch):
    monkeypatch.setenv("HERMES_MINIO_ENABLED", "true")
    monkeypatch.setenv("HERMES_MINIO_ENDPOINT", "minio.example:9000")
    monkeypatch.setenv("HERMES_MINIO_BUCKET", "b")
    fake_module = type("Fake", (), {})()
    fake_module.list_workspace_entries = lambda: [
        {"path": "alpha", "type": "dir", "size": 4096, "child_count": 3},
        {"path": "notes.txt", "type": "file", "size": 128},
    ]
    fake_module.compute_prefix_used_bytes = lambda: 0
    fake_module.validate_workspace_paths = lambda paths: list(paths or [])
    monkeypatch.setattr(bridge, "_load_minio_sync_module", lambda: fake_module)

    status = bridge.get_status()
    paths = [e["path"] for e in status["workspace_entries"]]
    assert paths == ["alpha", "notes.txt"]


def test_trigger_workspace_passes_paths_to_subprocess(monkeypatch):
    monkeypatch.setenv("HERMES_MINIO_ENABLED", "true")
    monkeypatch.setenv("HERMES_MINIO_ENDPOINT", "minio:9000")
    monkeypatch.setenv("HERMES_MINIO_BUCKET", "b")
    fake_module = type("Fake", (), {})()
    fake_module.validate_workspace_paths = lambda raw: list(raw or [])
    monkeypatch.setattr(bridge, "_load_minio_sync_module", lambda: fake_module)

    captured = {}

    def _fake_spawn(lane, args):
        captured["lane"] = lane
        captured["args"] = args
        return {"ok": True, "started": True, "lane": lane, "started_at": 0}

    monkeypatch.setattr(bridge, "_spawn_lane", _fake_spawn)
    res = bridge.trigger_workspace_sync(
        mode="safe", paths=["export", "projects/foo", "notes.txt"],
    )
    assert res["ok"] is True
    assert captured["args"][:3] == ["sync-workspace", "--mode", "safe"]
    assert "--paths" in captured["args"]
    paths_idx = captured["args"].index("--paths")
    assert captured["args"][paths_idx + 1: paths_idx + 4] == [
        "export", "projects/foo", "notes.txt",
    ]


def test_trigger_workspace_rejects_invalid_paths(monkeypatch):
    monkeypatch.setenv("HERMES_MINIO_ENABLED", "true")
    monkeypatch.setenv("HERMES_MINIO_ENDPOINT", "minio:9000")
    monkeypatch.setenv("HERMES_MINIO_BUCKET", "b")

    def _bad_validator(_paths):
        raise ValueError("path traversal not allowed: '..'")

    fake_module = type("Fake", (), {})()
    fake_module.validate_workspace_paths = _bad_validator
    monkeypatch.setattr(bridge, "_load_minio_sync_module", lambda: fake_module)

    res = bridge.trigger_workspace_sync(mode="safe", paths=["../etc/passwd"])
    assert res["ok"] is False
    assert "路径选择无效" in res["error"] or "invalid path" in res["error"].lower()


def test_trigger_workspace_no_paths_omits_flag(monkeypatch):
    """Passing no paths means "full workspace" — the --paths flag is dropped."""
    monkeypatch.setenv("HERMES_MINIO_ENABLED", "true")
    monkeypatch.setenv("HERMES_MINIO_ENDPOINT", "minio:9000")
    monkeypatch.setenv("HERMES_MINIO_BUCKET", "b")
    fake_module = type("Fake", (), {})()
    fake_module.validate_workspace_paths = lambda raw: list(raw or [])
    monkeypatch.setattr(bridge, "_load_minio_sync_module", lambda: fake_module)

    captured = {}

    def _fake_spawn(lane, args):
        captured["args"] = args
        return {"ok": True, "started": True, "lane": lane, "started_at": 0}

    monkeypatch.setattr(bridge, "_spawn_lane", _fake_spawn)
    bridge.trigger_workspace_sync(mode="safe")
    assert "--paths" not in captured["args"]
    bridge.trigger_workspace_sync(mode="safe", paths=[])
    assert "--paths" not in captured["args"]


def test_quota_env_invalid_value_treated_as_zero(monkeypatch):
    monkeypatch.setenv("HERMES_MINIO_QUOTA_BYTES", "not-a-number")
    assert bridge._quota_bytes() == 0
    monkeypatch.setenv("HERMES_MINIO_QUOTA_BYTES", "-100")
    assert bridge._quota_bytes() == 0
    monkeypatch.setenv("HERMES_MINIO_QUOTA_BYTES", "1024")
    assert bridge._quota_bytes() == 1024
    monkeypatch.delenv("HERMES_MINIO_QUOTA_BYTES")
    assert bridge._quota_bytes() == 0


def test_status_includes_blocked_extensions(monkeypatch):
    """Status payload exposes the configured blocked upload extensions."""
    monkeypatch.setenv("HERMES_MINIO_ENABLED", "true")
    monkeypatch.setenv("HERMES_MINIO_ENDPOINT", "minio.example:9000")
    monkeypatch.setenv("HERMES_MINIO_BUCKET", "hermes-state")
    monkeypatch.setenv("HERMES_MINIO_BLOCKED_EXTENSIONS", "pdf,zip,exe")
    # Reset module cache so new env is picked up
    bridge._minio_sync_module = None
    bridge._minio_sync_module_load_failed = False
    status = bridge.get_status()
    exts = status.get("blocked_extensions")
    assert isinstance(exts, list)
    assert sorted(exts) == ["exe", "pdf", "zip"]


def test_status_blocked_extensions_default(monkeypatch):
    """When no custom env is set, defaults are exposed."""
    monkeypatch.setenv("HERMES_MINIO_ENABLED", "true")
    monkeypatch.setenv("HERMES_MINIO_ENDPOINT", "minio.example:9000")
    monkeypatch.setenv("HERMES_MINIO_BUCKET", "hermes-state")
    monkeypatch.delenv("HERMES_MINIO_BLOCKED_EXTENSIONS", raising=False)
    bridge._minio_sync_module = None
    bridge._minio_sync_module_load_failed = False
    status = bridge.get_status()
    exts = status.get("blocked_extensions")
    assert isinstance(exts, list)
    assert "doc" in exts
    assert "xlsx" in exts


def test_status_blocked_extensions_empty_when_disabled(monkeypatch):
    """When MinIO is disabled, blocked_extensions is empty."""
    monkeypatch.delenv("HERMES_MINIO_ENABLED", raising=False)
    status = bridge.get_status()
    assert status.get("blocked_extensions") == []


# ── Remote file listing via bridge ─────────────────────────────────────────


def test_get_remote_files_when_disabled(monkeypatch):
    monkeypatch.delenv("HERMES_MINIO_ENABLED", raising=False)
    res = bridge.get_remote_files()
    assert res["ok"] is False
    assert res["files"] == []


def test_get_remote_files_when_configured(monkeypatch):
    monkeypatch.setenv("HERMES_MINIO_ENABLED", "true")
    monkeypatch.setenv("HERMES_MINIO_ENDPOINT", "minio:9000")
    monkeypatch.setenv("HERMES_MINIO_BUCKET", "b")
    fake_module = type("Fake", (), {})()
    fake_module.list_workspace_entries = lambda: []
    fake_module.compute_prefix_used_bytes = lambda: 0
    fake_module.compute_bucket_used_bytes = lambda: 0
    fake_module.validate_workspace_paths = lambda raw: list(raw or [])
    fake_module.list_remote_files = lambda: [
        {"path": "home/state.db", "size": 100, "last_modified": None},
        {"path": "workspace/a.txt", "size": 50, "last_modified": "2026-01-15T10:00:00+00:00"},
        {"path": "workspace/sub/b.md", "size": 30, "last_modified": None},
    ]
    monkeypatch.setattr(bridge, "_load_minio_sync_module", lambda: fake_module)
    res = bridge.get_remote_files()
    assert res["ok"] is True
    # Only workspace entries, home/ excluded
    assert len(res["files"]) == 2
    # Paths have workspace/ prefix stripped for display
    assert res["files"][0]["path"] == "a.txt"
    assert res["files"][1]["path"] == "sub/b.md"
    # Verify no home/ entries leak through
    paths = [f["path"] for f in res["files"]]
    assert not any(p.startswith("home/") for p in paths)
