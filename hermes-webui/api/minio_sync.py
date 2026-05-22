"""WebUI bridge for MinIO sync.

Provides a tiny orchestrator around ``scripts/minio_sync.py`` so the WebUI
can:

- read the current MinIO mode + last sync results,
- expose quota / used / remaining storage and a registration URL for users
  whose container does not yet have MinIO configured, and
- trigger a manual state or workspace sync (with selectable scope) without
  blocking the HTTP request.

Workspace sync is intentionally treated as a separate, explicit action with
a default-conservative mode (``safe`` / non-mirror, no remote deletion) and
scoped to user-selected paths when provided.

Design notes
~~~~~~~~~~~~

The command-line tool prints a final ``RESULT_JSON {...}`` line when it
finishes a sync, which we parse here. We deliberately never block the HTTP
worker thread: the request handler returns immediately with a "started"
acknowledgment, and the actual subprocess runs on a daemon thread. The
latest result for each lane (state / workspace) is kept in memory and
exposed via ``/api/minio/sync/status``.

We do **not** persist secrets here — config is read from environment
variables exactly as ``minio_sync.py`` does. The status endpoint returns
only the public, non-sensitive fields (endpoint host, bucket, prefix,
quota numbers, registration URL, and the workspace entry list).
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────

# scripts/minio_sync.py lives next to the repo root one level above
# hermes-webui/. The WebUI Dockerfile mounts both under
# /opt/hermes-offline, so this resolves both inside the container and in
# checkouts during dev.
_REPO_ROOT_CANDIDATES = [
    Path("/opt/hermes-offline"),
    Path(__file__).resolve().parent.parent.parent,
]


def _find_minio_sync_script() -> Path | None:
    for root in _REPO_ROOT_CANDIDATES:
        candidate = root / "scripts" / "minio_sync.py"
        if candidate.exists():
            return candidate
    return None


def _python_executable() -> str:
    return os.environ.get("HERMES_WEBUI_PYTHON") or sys.executable or "python3"


# ── Helpers for status payload ────────────────────────────────────────────


def _coerce_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() == "true"


def _public_config() -> dict[str, Any]:
    """Return a non-sensitive snapshot of the current MinIO configuration."""
    return {
        "enabled": _truthy(os.environ.get("HERMES_MINIO_ENABLED")),
        "endpoint": os.environ.get("HERMES_MINIO_ENDPOINT") or "",
        "bucket": os.environ.get("HERMES_MINIO_BUCKET") or "",
        "prefix": (os.environ.get("HERMES_MINIO_PREFIX") or "").strip("/"),
        "secure": _truthy(os.environ.get("HERMES_MINIO_SECURE")),
        "sync_interval_seconds": _coerce_int(
            os.environ.get("HERMES_MINIO_SYNC_INTERVAL"), 300
        ),
    }


def _quota_bytes() -> int:
    """Return the operator-configured per-user quota in bytes (``0`` = unset).

    Kept for backwards compatibility with callers/tests that expect to read
    the env override directly. Prefer :func:`_discover_quota` for the public
    status payload, which also reports whether the value came from real
    service discovery or from this fallback.
    """
    raw = os.environ.get("HERMES_MINIO_QUOTA_BYTES", "")
    try:
        value = int(str(raw).strip()) if raw else 0
    except ValueError:
        return 0
    return value if value > 0 else 0


def _discover_quota(configured: bool) -> tuple[int, str]:
    """Return ``(bytes, source)`` using service-derived discovery first.

    The fallback chain (admin API → bucket tag → env override) lives in
    :mod:`scripts.minio_sync.discover_quota`. We mirror it here for callers
    that only have access to the bridge module so they can render *why* a
    particular quota number is shown — operators should not have to guess
    whether the displayed number came from the live MinIO service or from
    a hand-set env override.

    When MinIO isn't configured we still honour an explicit ``HERMES_MINIO_QUOTA_BYTES``
    env variable so the WebUI can preview an operator's intended quota in
    the unavailable card if they choose to set one. In practice the
    unavailable card hides the quota anyway, but we keep the value
    consistent across paths.
    """
    if not configured:
        env = _quota_bytes()
        return (env, "env") if env > 0 else (0, "unset")
    module = _load_minio_sync_module()
    if module is not None and hasattr(module, "discover_quota"):
        try:
            quota, source = module.discover_quota()
            quota = int(quota)
            source = str(source or "unset")
            if quota > 0:
                return quota, source
        except Exception as exc:  # pragma: no cover - depends on remote
            logger.debug("discover_quota failed: %s", exc)
    # Module unavailable or returned unset: fall back to env directly.
    env = _quota_bytes()
    return (env, "env") if env > 0 else (0, "unset")


def _register_url() -> str:
    """Return the operator-configured registration URL for users without storage."""
    raw = os.environ.get("HERMES_MINIO_REGISTER_URL", "")
    return str(raw or "").strip()


def _config_completeness(cfg: dict[str, Any]) -> tuple[bool, str | None]:
    """Return ``(configured, reason_if_unavailable)`` for the public payload.

    "Configured" means MinIO mode is enabled AND the operator supplied the
    minimum identity to actually talk to a bucket. We don't check secret
    keys here because they're never read in this process — only emitted
    as env vars to the subprocess. Missing access/secret keys cause the
    sync subprocess to fail loudly; we don't want to leak whether they
    are set or not in the status payload.
    """
    if not cfg["enabled"]:
        return False, "此容器未启用 MinIO 同步"
    missing = []
    if not cfg["endpoint"]:
        missing.append("endpoint")
    if not cfg["bucket"]:
        missing.append("bucket")
    if missing:
        return False, "缺少 MinIO 配置项: " + ", ".join(missing)
    return True, None


# ── Lazy access to scripts/minio_sync.py for read-only metadata ───────────

_minio_sync_module = None
_minio_sync_module_load_failed = False


def _load_minio_sync_module():
    """Import scripts/minio_sync.py once for read-only metadata helpers.

    Imported lazily so the WebUI process doesn't pay the cost of loading the
    minio package on first request when MinIO is disabled.
    """
    global _minio_sync_module, _minio_sync_module_load_failed
    if _minio_sync_module is not None or _minio_sync_module_load_failed:
        return _minio_sync_module
    script = _find_minio_sync_script()
    if not script:
        _minio_sync_module_load_failed = True
        return None
    try:
        spec = importlib.util.spec_from_file_location("hermes_minio_sync", script)
        if spec is None or spec.loader is None:
            _minio_sync_module_load_failed = True
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        _minio_sync_module = module
        return module
    except Exception as exc:  # pragma: no cover - depends on environment
        logger.debug("Failed to load minio_sync module for metadata: %s", exc)
        _minio_sync_module_load_failed = True
        return None


def _blocked_extensions() -> list[str]:
    """Return the currently configured blocked upload extensions as a sorted list."""
    module = _load_minio_sync_module()
    if module is None or not hasattr(module, "get_blocked_extensions"):
        return []
    try:
        return sorted(module.get_blocked_extensions())
    except Exception:
        return []


def _list_workspace_entries() -> list[dict[str, Any]]:
    module = _load_minio_sync_module()
    if module is None or not hasattr(module, "list_workspace_entries"):
        return []
    try:
        return list(module.list_workspace_entries())
    except Exception as exc:  # pragma: no cover - filesystem errors are rare
        logger.debug("list_workspace_entries failed: %s", exc)
        return []


def _used_bytes(configured: bool) -> int:
    """Return the ENTIRE bucket's usage in bytes; ``0`` when unknown.

    Shows full bucket usage so users understand total consumed storage,
    not just their prefix. Falls back to prefix-level if bucket-level
    function is unavailable.
    """
    if not configured:
        return 0
    module = _load_minio_sync_module()
    if module is None:
        return 0
    # Prefer bucket-level usage for the '已用空间' display
    if hasattr(module, "compute_bucket_used_bytes"):
        try:
            return int(module.compute_bucket_used_bytes())
        except Exception as exc:  # pragma: no cover
            logger.debug("compute_bucket_used_bytes failed: %s", exc)
    # Fallback to prefix-level
    if hasattr(module, "compute_prefix_used_bytes"):
        try:
            return int(module.compute_prefix_used_bytes())
        except Exception as exc:  # pragma: no cover
            logger.debug("compute_prefix_used_bytes failed: %s", exc)
    return 0


def _validate_paths_locally(raw_paths) -> list[str]:
    """Pre-validate selected paths before spawning the subprocess.

    Catches obvious errors (path traversal, absolute paths, missing files) at
    the API boundary so the user sees a clean error instead of a dropped
    subprocess. The subprocess re-validates as defense-in-depth.
    """
    if not raw_paths:
        return []
    module = _load_minio_sync_module()
    if module is None or not hasattr(module, "validate_workspace_paths"):
        # If we can't load the validator, refuse rather than passing
        # untrusted strings to the subprocess.
        raise ValueError("path validation is unavailable")
    return list(module.validate_workspace_paths(raw_paths))


# ── In-memory run state ───────────────────────────────────────────────────

# Per-lane runtime state. Using two distinct lanes lets a quick state sync
# proceed even if a long workspace sync is still streaming.
_LANES = ("state", "workspace")
_lock = threading.Lock()
_running: dict[str, bool] = {lane: False for lane in _LANES}
_last_result: dict[str, dict[str, Any] | None] = {lane: None for lane in _LANES}


def _set_running(lane: str, value: bool) -> None:
    with _lock:
        _running[lane] = value


def _is_running(lane: str) -> bool:
    with _lock:
        return _running[lane]


def _record_result(lane: str, payload: dict[str, Any]) -> None:
    payload = {**payload, "finished_at": time.time()}
    with _lock:
        _last_result[lane] = payload


def _snapshot_status() -> dict[str, Any]:
    """Lightweight status snapshot — no bucket traversal or admin API calls.

    Expensive storage statistics (used_bytes, quota) are served by the
    separate ``get_usage()`` entry point so they are only computed when the
    user explicitly clicks "查询用量" in the UI.
    """
    cfg = _public_config()
    configured, unavailable_reason = _config_completeness(cfg)
    register_url = _register_url()
    entries = _list_workspace_entries()
    blocked_exts = _blocked_extensions() if configured else []
    with _lock:
        running = dict(_running)
        last = {lane: dict(v) if v else None for lane, v in _last_result.items()}
    script = _find_minio_sync_script()
    return {
        "config": cfg,
        "configured": configured,
        "unavailable_reason": unavailable_reason,
        "register_url": register_url,
        "workspace_entries": entries,
        "blocked_extensions": blocked_exts,
        "script_available": bool(script),
        "running": running,
        "last_result": last,
    }


def _compute_usage() -> dict[str, Any]:
    """Expensive: queries MinIO for quota + bucket usage.

    Only called on explicit user action (click "查询用量"), never during
    routine status polling.
    """
    cfg = _public_config()
    configured, _ = _config_completeness(cfg)
    quota, quota_source = _discover_quota(configured)
    used = _used_bytes(configured) if configured else 0
    if quota > 0:
        remaining = max(0, quota - used)
    else:
        remaining = 0
    return {
        "ok": True,
        "quota_bytes": quota,
        "quota_source": quota_source,
        "used_bytes": used,
        "remaining_bytes": remaining if quota > 0 else None,
    }


# ── Subprocess runner ─────────────────────────────────────────────────────


def _parse_result_line(stdout: str) -> dict[str, Any] | None:
    """Find the last ``RESULT_JSON {...}`` line emitted by minio_sync.py."""
    last: dict[str, Any] | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("RESULT_JSON "):
            continue
        try:
            last = json.loads(line[len("RESULT_JSON "):])
        except json.JSONDecodeError:
            continue
    return last


def _run_subprocess(args: list[str], timeout: float = 1800.0) -> dict[str, Any]:
    """Run minio_sync.py with the given args and capture a structured result."""
    script = _find_minio_sync_script()
    if not script:
        return {
            "ok": False,
            "error": "minio_sync.py script not found in expected locations",
        }

    cmd = [_python_executable(), str(script), *args]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"sync command timed out after {timeout:.0f}s",
            "duration_seconds": time.time() - started,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"sync command failed to launch: {exc}",
            "duration_seconds": time.time() - started,
        }

    duration = time.time() - started
    parsed = _parse_result_line(proc.stdout or "")
    base: dict[str, Any] = {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "duration_seconds": round(duration, 2),
    }
    if parsed:
        base["details"] = parsed
    if proc.returncode != 0:
        # Truncate stderr so a misbehaving subprocess can't dump unbounded text
        # into the response payload.
        stderr_tail = (proc.stderr or "").strip().splitlines()[-10:]
        base["error"] = "\n".join(stderr_tail) or "minio_sync.py exited non-zero"
    return base


# ── Lane orchestration ────────────────────────────────────────────────────


def _spawn_lane(lane: str, args: list[str]) -> dict[str, Any]:
    """Start a sync run on the given lane; refuse if one is already in flight."""
    cfg = _public_config()
    configured, unavailable_reason = _config_completeness(cfg)
    if not configured:
        return {
            "ok": False,
            "error": unavailable_reason or "MinIO 同步未配置",
        }
    if _is_running(lane):
        return {"ok": False, "error": f"{lane} 同步正在进行中"}

    started_at = time.time()

    def _runner() -> None:
        _set_running(lane, True)
        try:
            result = _run_subprocess(args)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("minio sync runner crashed")
            result = {"ok": False, "error": f"runner crashed: {exc}"}
        result.setdefault("started_at", started_at)
        result.setdefault("args", list(args))
        _record_result(lane, result)
        _set_running(lane, False)

    thread = threading.Thread(target=_runner, name=f"minio-sync-{lane}", daemon=True)
    thread.start()
    return {
        "ok": True,
        "started": True,
        "lane": lane,
        "started_at": started_at,
    }


# ── Public entry points (called from routes) ──────────────────────────────


def get_status() -> dict[str, Any]:
    return _snapshot_status()


def get_usage() -> dict[str, Any]:
    """Return storage usage + quota — expensive, only called on user click."""
    return _compute_usage()


def get_remote_files() -> dict[str, Any]:
    """List workspace files currently stored in MinIO (excludes home/ state)."""
    cfg = _public_config()
    configured, unavailable_reason = _config_completeness(cfg)
    if not configured:
        return {"ok": False, "error": unavailable_reason or "MinIO 未配置", "files": []}
    module = _load_minio_sync_module()
    if module is None or not hasattr(module, "list_remote_files"):
        return {"ok": False, "error": "list_remote_files 不可用", "files": []}
    try:
        all_files = list(module.list_remote_files())
        # Only return workspace entries; strip the "workspace/" prefix for display
        files = [
            {**f, "path": f["path"][len("workspace/"):]}
            for f in all_files
            if f.get("path", "").startswith("workspace/")
        ]
        return {"ok": True, "files": files}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "files": []}


def trigger_state_sync() -> dict[str, Any]:
    return _spawn_lane("state", ["sync-state"])


def trigger_workspace_sync(
    mode: str = "safe",
    cleanup_remote: bool = False,
    paths: list[str] | None = None,
) -> dict[str, Any]:
    """Validate options then dispatch a workspace sync run."""
    mode = (mode or "safe").strip().lower()
    if mode not in ("safe", "mirror"):
        return {"ok": False, "error": "mode 必须为 'safe' 或 'mirror'"}
    if cleanup_remote and mode != "mirror":
        return {"ok": False, "error": "cleanup_remote 需要 mode='mirror'"}

    try:
        validated_paths = _validate_paths_locally(paths)
    except ValueError as exc:
        return {"ok": False, "error": f"路径选择无效: {exc}"}

    args = ["sync-workspace", "--mode", mode]
    if cleanup_remote:
        args.append("--cleanup-remote")
    if validated_paths:
        args.append("--paths")
        args.extend(validated_paths)
    return _spawn_lane("workspace", args)
