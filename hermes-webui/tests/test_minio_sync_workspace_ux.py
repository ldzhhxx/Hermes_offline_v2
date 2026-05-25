"""MinIO sync UX placement + auto-discovery quota tests.

Covers the regressions called out in the production-UX correction:

1. The MinIO panel must be exposed in the workspace right-side area (not
   buried under analytics).
2. Both unavailable and configured states must render via the same
   workspace mount point, with the registration link surfaced when the
   user's container does not have MinIO configured.
3. Quota must auto-discover (admin API → bucket tag → env override → unset)
   so operators are not forced to fill HERMES_MINIO_QUOTA_BYTES manually.

These tests mix two styles:

* Static-asset assertions for the HTML / JS placement (cheaper than a full
  Selenium round-trip and they run in the regular pytest suite without an
  extra browser dep). These match the existing pattern in
  ``test_workspace_blank_page_fix.py``.
* Bridge-level pytest assertions for the new ``quota_source`` semantics.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from api import minio_sync as bridge  # noqa: E402


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_lanes():
    """Each test starts from a clean lane state."""
    with bridge._lock:
        for lane in bridge._LANES:
            bridge._running[lane] = False
            bridge._last_result[lane] = None
    bridge._credential_probe_cache["expires_at"] = 0.0
    bridge._credential_probe_cache["result"] = None
    yield
    with bridge._lock:
        for lane in bridge._LANES:
            bridge._running[lane] = False
            bridge._last_result[lane] = None
    bridge._credential_probe_cache["expires_at"] = 0.0
    bridge._credential_probe_cache["result"] = None


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


# ── 1) Workspace right-side placement ──────────────────────────────────────


class TestWorkspaceMountVisibility:
    """The MinIO panel must mount in the workspace right-side area."""

    def test_index_html_has_workspace_mount_inside_rightpanel(self):
        """The mount point must be inside <aside class="rightpanel">.

        This is the structural anchor the user asked for: visible in the
        right-side workspace area, not somewhere inside the analytics tab.
        """
        html = _read("static/index.html")
        # The mount element exists by name and class.
        assert 'id="workspaceMinioSyncMount"' in html, (
            "Workspace MinIO sync mount missing from index.html. The MinIO "
            "panel must render into a stable mount inside the workspace "
            "right-side area, not be tucked away in the Insights tab."
        )
        assert 'class="workspace-cloud-sync"' in html

        # And it's inside the right-side <aside class="rightpanel">.
        aside_idx = html.find('<aside class="rightpanel">')
        mount_idx = html.find('id="workspaceMinioSyncMount"')
        aside_end = html.find("</aside>", aside_idx)
        assert aside_idx >= 0 and aside_end >= 0
        assert aside_idx < mount_idx < aside_end, (
            "Mount must be between <aside class='rightpanel'> and its </aside>"
        )

    def test_panels_js_renders_into_workspace_mount(self):
        """`refreshMinioSyncStatus` must target the workspace mount, not
        the legacy Insights-internal #minioSyncPanel host element."""
        js = _read("static/panels.js")
        assert "workspaceMinioSyncMount" in js, (
            "panels.js must render MinIO sync into the workspace right-side "
            "mount (#workspaceMinioSyncMount)."
        )
        assert "payload.registration_required" in js, (
            "The workspace MinIO renderer must respect registration_required so invalid AK/SK still show the registration CTA."
        )
        # The previous code used getElementById('minioSyncPanel') as the
        # render host. Make sure we don't regress to that pattern.
        assert "getElementById('minioSyncPanel')" not in js, (
            "Found legacy reference to #minioSyncPanel as a render host. The "
            "panel now lives at #workspaceMinioSyncMount; getting it from "
            "#minioSyncPanel will silently lose the workspace placement."
        )
        assert "当前 MinIO 凭证无效，需重新注册或申请存储空间" in js, (
            "Unavailable card should explain the credential-invalid registration state in Chinese."
        )

    def test_minio_panel_not_rendered_inside_insights_box(self):
        """`_renderInsights` must no longer emit MinIO panel markup.

        Without this guard, the panel would double-render (once inside the
        analytics card list, once in the workspace area), and the analytics
        copy was the one users could not find.
        """
        js = _read("static/panels.js")
        # Find the _renderInsights body and verify it does not call the
        # MinIO renderer.
        start = js.find("function _renderInsights(")
        assert start >= 0, "_renderInsights not found"
        # Capture the function body up to the next top-level function.
        end = js.find("\nasync function clearConversation", start)
        if end < 0:
            end = js.find("\nasync function", start + 1)
        body = js[start:end]
        assert "_renderMinioSyncPanel(" not in body, (
            "_renderInsights must not call _renderMinioSyncPanel — that is "
            "the misplaced location the user asked us to remove."
        )

    def test_load_insights_does_not_request_minio_status(self):
        """Insights tab must not refetch MinIO status — that endpoint is now
        owned by the workspace mount, which avoids an extra round-trip per
        analytics refresh."""
        js = _read("static/panels.js")
        start = js.find("async function loadInsights(")
        assert start >= 0
        end = js.find("\nfunction _formatLlmWikiTimestamp", start)
        body = js[start:end]
        assert "/api/minio/sync/status" not in body, (
            "loadInsights() should not call /api/minio/sync/status — the "
            "workspace mount handles that. Re-fetching here keeps the "
            "panel duplicated in the wrong place."
        )

    def test_panels_js_bootstraps_mount_on_dom_ready(self):
        """The mount must boot autonomously so the unavailable card is
        visible from first paint, not only after the user happens to
        navigate to a panel that triggers it."""
        js = _read("static/panels.js")
        assert "mountWorkspaceMinioSync" in js
        # Either DOMContentLoaded or an immediate setTimeout(0) call: both
        # accomplish the goal under defer-loaded scripts.
        assert (
            "DOMContentLoaded" in js and "mountWorkspaceMinioSync" in js
        ) or "setTimeout(start, 0)" in js


# ── 2) Unavailable vs configured rendering paths ───────────────────────────


class TestUnavailableVsConfiguredPaths:
    """Both states must render via the same workspace mount."""

    def _stub_metadata(self, monkeypatch, used=0, entries=None, quota_source=None,
                       quota_bytes=0):
        fake = type("Fake", (), {})()
        fake.list_workspace_entries = lambda: list(entries or [])
        fake.compute_prefix_used_bytes = lambda: int(used)
        fake.validate_workspace_paths = lambda raw: list(raw or [])
        if quota_source is not None:
            fake.discover_quota = lambda: (int(quota_bytes), str(quota_source))
        else:
            # Mimic an older build that doesn't expose discover_quota; the
            # bridge must still degrade gracefully.
            pass
        monkeypatch.setattr(bridge, "_load_minio_sync_module", lambda: fake)

    def test_unavailable_payload_has_register_url_for_workspace_card(
        self, monkeypatch
    ):
        """Unavailable state must surface enough data for the workspace
        mount to render the registration CTA."""
        monkeypatch.delenv("HERMES_MINIO_ENABLED", raising=False)
        monkeypatch.setenv(
            "HERMES_MINIO_REGISTER_URL", "https://internal.example/storage"
        )
        status = bridge.get_status()
        assert status["configured"] is False
        assert status["unavailable_reason"]
        assert status["register_url"] == "https://internal.example/storage"
        # The workspace renderer keys off `configured` / registration state to pick
        # the unavailable card; ensure these stay simple booleans.
        assert isinstance(status["configured"], bool)
        assert isinstance(status.get("registration_required", False), bool)

    def test_unavailable_when_only_partial_config(self, monkeypatch):
        """Enabled but missing endpoint/bucket must still report unavailable
        with a precise reason — not a generic 'configured: false'."""
        monkeypatch.setenv("HERMES_MINIO_ENABLED", "true")
        monkeypatch.delenv("HERMES_MINIO_ENDPOINT", raising=False)
        monkeypatch.delenv("HERMES_MINIO_BUCKET", raising=False)
        status = bridge.get_status()
        assert status["configured"] is False
        reason = (status["unavailable_reason"] or "")
        assert "缺少" in reason or "missing" in reason.lower()
        assert "endpoint" in reason.lower() or "bucket" in reason.lower()

    def test_invalid_credentials_still_surface_registration_cta_payload(self, monkeypatch):
        """If AK/SK are present but rejected, the payload should still drive the
        workspace registration CTA rather than rendering the normal configured panel."""
        monkeypatch.setenv("HERMES_MINIO_ENABLED", "true")
        monkeypatch.setenv("HERMES_MINIO_ENDPOINT", "minio.example:9000")
        monkeypatch.setenv("HERMES_MINIO_BUCKET", "hermes-state")
        monkeypatch.setenv(
            "HERMES_MINIO_REGISTER_URL", "https://internal.example/storage"
        )

        class AuthError(Exception):
            code = "SignatureDoesNotMatch"

        class FakeClient:
            def bucket_exists(self, _bucket):
                raise AuthError("The request signature we calculated does not match")

        fake = type("Fake", (), {})()
        fake.get_client = lambda: FakeClient()
        fake.list_workspace_entries = lambda: []
        fake.compute_prefix_used_bytes = lambda: 0
        fake.validate_workspace_paths = lambda raw: list(raw or [])
        fake.get_blocked_extensions = lambda: frozenset()
        monkeypatch.setattr(bridge, "_load_minio_sync_module", lambda: fake)

        status = bridge.get_status()
        assert status["configured"] is True
        assert status["registration_required"] is True
        assert status["register_url"] == "https://internal.example/storage"
        assert status["registration_state"] == "credential_invalid"

    def test_configured_payload_renders_with_workspace_entries(
        self, monkeypatch
    ):
        """Configured state surfaces entries + usage — the data the
        workspace mount needs to render selectable scope."""
        monkeypatch.setenv("HERMES_MINIO_ENABLED", "true")
        monkeypatch.setenv("HERMES_MINIO_ENDPOINT", "minio.example:9000")
        monkeypatch.setenv("HERMES_MINIO_BUCKET", "hermes-state")
        self._stub_metadata(
            monkeypatch,
            used=128,
            entries=[
                {"path": "alpha", "type": "dir", "size": 4096, "child_count": 2},
                {"path": "n.txt", "type": "file", "size": 64},
            ],
            quota_source="unset",
            quota_bytes=0,
        )
        status = bridge.get_status()
        assert status["configured"] is True
        assert status["unavailable_reason"] is None
        assert [e["path"] for e in status["workspace_entries"]] == [
            "alpha", "n.txt",
        ]


# ── 3) Quota auto-discovery / fallback semantics ───────────────────────────


class TestQuotaAutoDiscoverySemantics:
    """The bridge must prefer service-derived quota and only fall back to
    HERMES_MINIO_QUOTA_BYTES as an explicit operator override.

    The `quota_source` field tells the WebUI which path produced the
    number so operators can tell discovery from override at a glance.

    Since quota/usage are now served by `get_usage()` (click-to-request),
    these tests exercise that endpoint rather than `get_status()`.
    """

    def _stub_module(self, monkeypatch, *, discover_value):
        """Install a fake script-module that returns the given discovery."""
        fake = type("Fake", (), {})()
        fake.list_workspace_entries = lambda: []
        fake.compute_prefix_used_bytes = lambda: 0
        fake.validate_workspace_paths = lambda raw: list(raw or [])
        fake.discover_quota = lambda: discover_value
        monkeypatch.setattr(bridge, "_load_minio_sync_module", lambda: fake)
        # And pretend MinIO is configured so _discover_quota takes the
        # service-derived path rather than the env-only short-circuit.
        monkeypatch.setenv("HERMES_MINIO_ENABLED", "true")
        monkeypatch.setenv("HERMES_MINIO_ENDPOINT", "minio:9000")
        monkeypatch.setenv("HERMES_MINIO_BUCKET", "b")

    def test_admin_api_takes_precedence_over_env(self, monkeypatch):
        monkeypatch.setenv("HERMES_MINIO_QUOTA_BYTES", str(5 * 1024**3))
        self._stub_module(monkeypatch, discover_value=(20 * 1024**3, "admin_api"))
        usage = bridge.get_usage()
        assert usage["quota_bytes"] == 20 * 1024**3
        assert usage["quota_source"] == "admin_api", (
            "admin_api result must take precedence over the env fallback"
        )

    def test_bucket_tag_takes_precedence_over_env(self, monkeypatch):
        monkeypatch.setenv("HERMES_MINIO_QUOTA_BYTES", str(5 * 1024**3))
        self._stub_module(
            monkeypatch, discover_value=(15 * 1024**3, "bucket_tag")
        )
        usage = bridge.get_usage()
        assert usage["quota_bytes"] == 15 * 1024**3
        assert usage["quota_source"] == "bucket_tag"

    def test_falls_back_to_env_when_discovery_unset(self, monkeypatch):
        """When live discovery returns 0/unset, the env override is the
        last-resort fallback — labeled as such."""
        monkeypatch.setenv("HERMES_MINIO_QUOTA_BYTES", str(7 * 1024**3))
        self._stub_module(monkeypatch, discover_value=(0, "unset"))
        usage = bridge.get_usage()
        assert usage["quota_bytes"] == 7 * 1024**3
        assert usage["quota_source"] == "env"

    def test_unset_when_neither_discovery_nor_env_provides(self, monkeypatch):
        monkeypatch.delenv("HERMES_MINIO_QUOTA_BYTES", raising=False)
        self._stub_module(monkeypatch, discover_value=(0, "unset"))
        usage = bridge.get_usage()
        assert usage["quota_bytes"] == 0
        assert usage["quota_source"] == "unset"
        # Remaining is null so the UI can show "—" rather than an inflated
        # number.
        assert usage["remaining_bytes"] is None

    def test_unavailable_minio_keeps_env_override_visible_as_env(
        self, monkeypatch
    ):
        """If MinIO is not enabled at all, the bridge cannot run discovery —
        any env override should still be reported with source='env' so
        the source label remains accurate end-to-end."""
        monkeypatch.delenv("HERMES_MINIO_ENABLED", raising=False)
        monkeypatch.setenv("HERMES_MINIO_QUOTA_BYTES", str(3 * 1024**3))
        usage = bridge.get_usage()
        assert usage["quota_bytes"] == 3 * 1024**3
        assert usage["quota_source"] == "env"

    def test_module_without_discover_quota_falls_back_to_env(self, monkeypatch):
        """Old script module without `discover_quota` must not crash the
        bridge — env-only behaviour is preserved as the final fallback."""
        fake = type("Fake", (), {})()
        fake.list_workspace_entries = lambda: []
        fake.compute_prefix_used_bytes = lambda: 0
        fake.validate_workspace_paths = lambda raw: list(raw or [])
        # No `discover_quota` attribute deliberately.
        monkeypatch.setattr(bridge, "_load_minio_sync_module", lambda: fake)
        monkeypatch.setenv("HERMES_MINIO_ENABLED", "true")
        monkeypatch.setenv("HERMES_MINIO_ENDPOINT", "minio:9000")
        monkeypatch.setenv("HERMES_MINIO_BUCKET", "b")
        monkeypatch.setenv("HERMES_MINIO_QUOTA_BYTES", str(2 * 1024**3))
        usage = bridge.get_usage()
        assert usage["quota_bytes"] == 2 * 1024**3
        assert usage["quota_source"] == "env"

    def test_status_endpoint_no_longer_includes_usage_fields(self, monkeypatch):
        """get_status() must NOT include quota/usage fields — they are now
        lazy-loaded via get_usage() to avoid expensive bucket scans on
        every panel refresh."""
        monkeypatch.setenv("HERMES_MINIO_ENABLED", "true")
        monkeypatch.setenv("HERMES_MINIO_ENDPOINT", "minio:9000")
        monkeypatch.setenv("HERMES_MINIO_BUCKET", "b")
        self._stub_module(monkeypatch, discover_value=(10 * 1024**3, "admin_api"))
        status = bridge.get_status()
        assert "quota_bytes" not in status
        assert "used_bytes" not in status
        assert "remaining_bytes" not in status
        assert "quota_source" not in status


# ── 4) Workspace mount renderer markup includes quota_source label ─────────


class TestQuotaSourceLabelMarkup:
    """The renderer must label env-derived quotas explicitly as the
    operator-override fallback so users don't mistake them for live
    service-derived data."""

    def test_panels_js_emits_quota_source_attribute(self):
        js = _read("static/panels.js")
        assert "minio-sync-quota-source" in js, (
            "Renderer must emit a `.minio-sync-quota-source` block so the "
            "UI can surface where the displayed quota came from."
        )
        assert "运维覆盖" in js, (
            "Env-derived quota must be labeled as operator override (运维覆盖) so "
            "operators can tell it apart from auto-discovery."
        )
        assert "自动发现" in js, (
            "Discovered quota (admin API / bucket tag) must be labeled "
            "as auto-discovered (自动发现)."
        )


class TestMinioGuidanceAndBlockedExtensions:
    """Tests for the inline guidance and blocked-extension visibility."""

    def test_panels_js_contains_guidance_function(self):
        js = _read("static/panels.js")
        assert "_renderMinioGuidance" in js, (
            "panels.js must define _renderMinioGuidance for inline help."
        )

    def test_guidance_text_mentions_key_rules(self):
        js = _read("static/panels.js")
        # Key guidance items in Chinese
        assert "自动定时同步" in js or "自动同步" in js, "Should mention auto state sync"
        assert "不会自动同步" in js, "Should explain workspace is manual only"
        assert "全量覆盖" in js, "Should explain full sync semantics"
        assert "仅所选" in js or "仅上传" in js, "Should explain upload-selected semantics"
        assert "不会删除您的本地文件" in js, "Should reassure local files are safe"
        assert "禁传扩展名" in js or "禁传" in js, "Should mention blocked extensions"

    def test_sync_summary_shows_blocked_count(self):
        js = _read("static/panels.js")
        assert "因扩展名被禁传" in js, (
            "Sync summary should explain blocked files in Chinese."
        )

    def test_guidance_renders_blocked_ext_list_from_payload(self):
        js = _read("static/panels.js")
        assert "blocked_extensions" in js, (
            "panels.js must read blocked_extensions from the status payload."
        )

    def test_css_has_guidance_styles(self):
        css = _read("static/style.css")
        assert ".minio-sync-guidance" in css, (
            "style.css must style the guidance block."
        )


class TestMinioTimestampAndIntervalDisplay:
    """Tests for MinIO panel relative-time display and auto-sync interval text."""

    def test_minio_timestamp_uses_shared_relative_formatter(self):
        js = _read("static/panels.js")
        assert "function _formatMinioRelativeTimestamp" in js, (
            "MinIO panel should expose a relative-time formatter for sync timestamps."
        )
        assert "_formatSyncRelativeTime" in js, (
            "MinIO panel should reuse the shared Chinese relative-time formatter."
        )

    def test_minio_result_timestamp_keeps_absolute_time_in_tooltip(self):
        js = _read("static/panels.js")
        assert "title=\"${esc(tsTitle)}\"" in js, (
            "Rendered MinIO sync result timestamps should keep the absolute time in a tooltip."
        )

    def test_state_row_shows_sync_interval(self):
        js = _read("static/panels.js")
        assert "sync_interval_seconds" in js, (
            "The Hermes state row must display the configured sync interval."
        )
        assert "秒自动同步一次" in js, (
            "The Hermes state row must explain auto-sync in Chinese."
        )

    def test_state_row_shows_last_sync_time(self):
        js = _read("static/panels.js")
        assert "上次同步" in js, (
            "The Hermes state row must show last sync time in Chinese."
        )
        assert "_formatMinioRelativeTimestamp(last.state.finished_at)" in js, (
            "The Hermes state row should use relative time as the primary visible timestamp."
        )

    def test_backend_exposes_sync_interval(self):
        """_public_config must include sync_interval_seconds."""
        import os
        old = os.environ.get("HERMES_MINIO_SYNC_INTERVAL")
        try:
            os.environ["HERMES_MINIO_SYNC_INTERVAL"] = "600"
            cfg = bridge._public_config()
            assert cfg["sync_interval_seconds"] == 600
        finally:
            if old is None:
                os.environ.pop("HERMES_MINIO_SYNC_INTERVAL", None)
            else:
                os.environ["HERMES_MINIO_SYNC_INTERVAL"] = old

    def test_backend_sync_interval_default(self):
        """Default sync interval is 300 when env is not set."""
        import os
        old = os.environ.pop("HERMES_MINIO_SYNC_INTERVAL", None)
        try:
            cfg = bridge._public_config()
            assert cfg["sync_interval_seconds"] == 300
        finally:
            if old is not None:
                os.environ["HERMES_MINIO_SYNC_INTERVAL"] = old


# ── 5) Details open state preservation across refreshes ────────────────────


class TestDetailsStatePreservation:
    """Regression: refreshMinioSyncStatus() must preserve the <details open>
    state and remote file list visibility across re-renders (mount.innerHTML
    replacement). Without the fix, clicking any action button collapses the
    expanded details section."""

    def test_refresh_preserves_details_open_attribute(self):
        """The refresh function must read the previous open state from the
        DOM before innerHTML replacement, and restore it after."""
        js = _read("static/panels.js")
        # Must query existing details element's open state before overwrite
        assert "prevDetails" in js or "wasOpen" in js, (
            "refreshMinioSyncStatus must capture details open state before "
            "innerHTML replacement."
        )
        # Must restore open attribute after innerHTML
        assert ".open = true" in js or ".open=true" in js, (
            "refreshMinioSyncStatus must restore details open state after "
            "innerHTML replacement."
        )

    def test_refresh_preserves_remote_files_visibility(self):
        """The remote file list must remain visible after a refresh if it
        was shown before the refresh."""
        js = _read("static/panels.js")
        assert "remoteWasVisible" in js or "remoteHtml" in js, (
            "refreshMinioSyncStatus must preserve remote file list "
            "visibility across re-renders."
        )

    def test_details_id_is_minioSyncDetails(self):
        """The <details> element must have id='minioSyncDetails' for the
        state preservation logic to work."""
        js = _read("static/panels.js")
        assert 'id="minioSyncDetails"' in js or "id='minioSyncDetails'" in js

    def test_remote_files_id_is_minioRemoteFiles(self):
        """The remote files container must have id='minioRemoteFiles'."""
        js = _read("static/panels.js")
        assert 'id="minioRemoteFiles"' in js or "id='minioRemoteFiles'" in js

    def test_state_capture_before_innerhtml_assignment(self):
        """The state capture code must appear BEFORE the innerHTML assignment
        in the source, not after (ordering matters)."""
        js = _read("static/panels.js")
        # Find the refresh function
        start = js.find("async function refreshMinioSyncStatus")
        assert start >= 0
        end = js.find("\nasync function mountWorkspaceMinioSync", start)
        body = js[start:end]
        # wasOpen capture must precede innerHTML assignment
        capture_pos = body.find("wasOpen")
        assign_pos = body.find("mount.innerHTML = html")
        assert capture_pos > 0 and assign_pos > 0
        assert capture_pos < assign_pos, (
            "Details open state must be captured BEFORE innerHTML is replaced."
        )
