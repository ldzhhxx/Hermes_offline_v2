from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import patch


class _FakeHandler:
    pass


def _install_fake_file_sync_module(progress: dict) -> ModuleType:
    module = ModuleType("tools.environments.file_sync")
    module._sync_progress = progress
    sys.modules["tools.environments.file_sync"] = module
    return module


def test_workspace_sync_progress_route_returns_current_progress_state():
    import api.routes as routes

    fake_progress = {
        "active": True,
        "stage": "uploading",
        "total": 9,
        "completed": 4,
        "deleted": 1,
        "current_file": "/tmp/demo.txt",
        "success": None,
        "last_error": None,
        "started_at": 123.0,
        "finished_at": None,
    }
    _install_fake_file_sync_module(fake_progress)

    captured = {}

    def fake_j(handler, data, status=200, extra_headers=None):
        captured["data"] = data
        captured["status"] = status
        return True

    with patch("api.routes.j", side_effect=fake_j):
        handled = routes.handle_get(_FakeHandler(), urlparse("http://example.test/api/workspace/sync-progress"))

    assert handled is True
    assert captured["status"] == 200
    assert captured["data"]["stage"] == "uploading"
    assert captured["data"]["total"] == 9
    assert captured["data"]["completed"] == 4
    assert captured["data"]["current_file"] == "/tmp/demo.txt"


def test_workspace_sync_route_returns_unavailable_when_no_active_sync_manager():
    import api.routes as routes

    fake_progress = {
        "active": True,
        "stage": "uploading",
        "total": 9,
        "completed": 4,
        "deleted": 1,
        "current_file": "/tmp/demo.txt",
        "success": None,
        "last_error": None,
        "started_at": 123.0,
        "finished_at": None,
    }
    _install_fake_file_sync_module(fake_progress)

    captured = {}

    def fake_j(handler, data, status=200, extra_headers=None):
        captured["data"] = data
        captured["status"] = status
        return True

    with patch("api.routes.j", side_effect=fake_j):
        result = routes._handle_workspace_sync(_FakeHandler(), {})

    assert result is True
    assert captured["status"] == 200
    assert captured["data"]["ok"] is False
    assert "No active remote environment" in captured["data"]["reason"]


def test_workspace_sync_route_starts_background_sync_when_manager_exists():
    import api.routes as routes

    fake_progress = {
        "active": False,
        "stage": "idle",
        "total": 0,
        "completed": 0,
        "deleted": 0,
        "current_file": None,
        "success": None,
        "last_error": None,
        "started_at": None,
        "finished_at": None,
    }
    _install_fake_file_sync_module(fake_progress)

    called = threading.Event()
    received = {}

    class _FakeManager:
        def sync(self, *, force=False, _report_progress=False):
            received["force"] = force
            received["report_progress"] = _report_progress
            called.set()

    fake_terminal_tool = ModuleType("tools.terminal_tool")
    fake_terminal_tool._active_environments = {"demo": SimpleNamespace(_sync_manager=_FakeManager())}
    fake_terminal_tool._env_lock = threading.Lock()
    sys.modules["tools.terminal_tool"] = fake_terminal_tool

    captured = {}

    def fake_j(handler, data, status=200, extra_headers=None):
        captured["data"] = data
        captured["status"] = status
        return True

    with patch("api.routes.j", side_effect=fake_j):
        result = routes._handle_workspace_sync(_FakeHandler(), {})

    assert result is True
    assert captured["status"] == 200
    assert captured["data"] == {"ok": True, "started": True}
    assert called.wait(1.0) is True
    assert received == {"force": True, "report_progress": True}


def test_workspace_sync_button_and_polling_ui_are_wired_in_static_assets():
    repo = Path(__file__).resolve().parents[1]
    index_html = (repo / "static" / "index.html").read_text(encoding="utf-8")
    panels_js = (repo / "static" / "panels.js").read_text(encoding="utf-8")

    assert 'id="btnSyncWorkspaceDetail"' in index_html
    assert 'onclick="syncCurrentWorkspace()"' in index_html
    assert "async function syncCurrentWorkspace()" in panels_js
    assert "api('/api/workspace/sync'" in panels_js
    assert "api('/api/workspace/sync-progress')" in panels_js
    assert "function _showSyncProgress(prog)" in panels_js
    assert "Uploading ${completed}/${total}" in panels_js
