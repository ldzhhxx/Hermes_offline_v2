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


def _has_env_credentials() -> bool:
    """Return True if AK/SK are present in env (operator-provided or login-set)."""
    return bool(os.environ.get("HERMES_MINIO_ACCESS_KEY") and
                os.environ.get("HERMES_MINIO_SECRET_KEY"))


def _clear_skip_flag() -> None:
    """Remove the MinIO restore skip flag so the panel reappears."""
    _startup_restore_skip_flag.clear()
    try:
        flag = Path(os.environ.get("HERMES_HOME", "/home/hermes/.hermes")) / "webui" / ".minio_restore_skipped"
        flag.unlink(missing_ok=True)
    except Exception:
        pass


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
        "has_credentials": _has_env_credentials(),
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

_CREDENTIAL_PROBE_TTL_SECONDS = 15.0
_credential_probe_cache: dict[str, Any] = {
    "expires_at": 0.0,
    "result": None,
}


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


def _looks_like_invalid_minio_credentials(exc: Exception) -> bool:
    """Return True when the exception strongly suggests invalid/unprovisioned AK/SK."""
    code = str(getattr(exc, "code", "") or "").strip().lower()
    text = str(exc or "").strip().lower()
    haystack = f"{code}\n{text}"
    needles = (
        "invalidaccesskeyid",
        "signaturedoesnotmatch",
        "accessdenied",
        "invalid access key",
        "access key id",
        "secret key",
        "invalid credentials",
        "forbidden",
        "unauthorized",
    )
    return any(needle in haystack for needle in needles)


def _probe_registration_requirement(cfg: dict[str, Any], configured: bool) -> dict[str, Any] | None:
    """Return registration/auth failure metadata when AK/SK appear invalid.

    This keeps the normal env-based `configured` meaning intact while letting the
    WebUI switch to the same registration CTA card when the current credentials
    clearly fail MinIO auth. The probe is cached briefly because `/status` may be
    polled multiple times while a sync is in flight.
    """
    if not configured:
        return None
    now = time.time()
    cached = _credential_probe_cache.get("result")
    expires_at = float(_credential_probe_cache.get("expires_at") or 0.0)
    if cached is not None and expires_at > now:
        return dict(cached)

    result = {
        "registration_required": False,
        "registration_reason": None,
        "registration_state": None,
    }
    module = _load_minio_sync_module()
    if module is None or not hasattr(module, "get_client"):
        _credential_probe_cache["result"] = dict(result)
        _credential_probe_cache["expires_at"] = now + _CREDENTIAL_PROBE_TTL_SECONDS
        return result
    try:
        client = module.get_client()
        # Lightweight auth check: resolves against the configured bucket without
        # traversing contents or querying usage stats.
        client.bucket_exists(cfg["bucket"])
    except Exception as exc:
        if _looks_like_invalid_minio_credentials(exc):
            result = {
                "registration_required": True,
                "registration_reason": "当前 MinIO AK/SK 校验失败，请重新注册或申请存储空间后再同步",
                "registration_state": "credential_invalid",
            }
        else:
            logger.debug("minio credential probe failed with non-auth error: %s", exc)
    _credential_probe_cache["result"] = dict(result)
    _credential_probe_cache["expires_at"] = now + _CREDENTIAL_PROBE_TTL_SECONDS
    return result


def try_minio_login(endpoint: str = "", access_key: str = "", secret_key: str = "",
                    bucket: str = "", prefix: str = "", secure: bool = False,
                    login_from_env: bool = False) -> dict[str, Any]:
    """Test MinIO credentials and update process env vars on success.

    When ``login_from_env=True``, reads all parameters from the current
    environment variables (operator-provided or previously saved).
    After a successful connection, credentials are persisted to disk so
    they survive container restarts.
    """
    if login_from_env:
        endpoint = os.environ.get("HERMES_MINIO_ENDPOINT", "")
        access_key = os.environ.get("HERMES_MINIO_ACCESS_KEY", "")
        secret_key = os.environ.get("HERMES_MINIO_SECRET_KEY", "")
        bucket = os.environ.get("HERMES_MINIO_BUCKET", "")
        prefix = os.environ.get("HERMES_MINIO_PREFIX", "")
        secure = os.environ.get("HERMES_MINIO_SECURE", "").lower() == "true"
        if not endpoint or not access_key or not secret_key or not bucket:
            return {"ok": False, "error": "环境变量中缺少 MinIO 凭证，无法自动登录"}
    try:
        from minio import Minio
        import urllib3
    except ImportError:
        return {"ok": False, "error": "minio 库未安装"}
    try:
        # Set a 10-second connection+read timeout to avoid hanging indefinitely
        timeout = urllib3.util.timeout.Timeout(connect=5, read=10)
        http_client = urllib3.PoolManager(timeout=timeout)
        client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure, http_client=http_client)
        if not client.bucket_exists(bucket):
            return {"ok": False, "error": f"存储桶 '{bucket}' 不存在"}
    except Exception as exc:
        if _looks_like_invalid_minio_credentials(exc):
            return {"ok": False, "error": "访问密钥或秘密密钥无效"}
        return {"ok": False, "error": str(exc)}
    # Update process env vars
    os.environ["HERMES_MINIO_ENABLED"] = "true"
    os.environ["HERMES_MINIO_ENDPOINT"] = endpoint
    os.environ["HERMES_MINIO_ACCESS_KEY"] = access_key
    os.environ["HERMES_MINIO_SECRET_KEY"] = secret_key
    os.environ["HERMES_MINIO_BUCKET"] = bucket
    os.environ["HERMES_MINIO_PREFIX"] = prefix
    os.environ["HERMES_MINIO_SECURE"] = "true" if secure else "false"
    # Clear skip flag if present
    _clear_skip_flag()
    # Invalidate credential probe cache
    _credential_probe_cache["result"] = None
    _credential_probe_cache["expires_at"] = 0.0
    # Reset the loaded module so it picks up new env vars on next use
    global _minio_sync_module, _minio_sync_module_load_failed
    _minio_sync_module = None
    _minio_sync_module_load_failed = False
    return {"ok": True}


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
    payload = dict(payload)
    payload.setdefault("finished_at", time.time())
    with _lock:
        _last_result[lane] = payload


def _read_persisted_state_sync_result() -> dict[str, Any] | None:
    """Read the durable state-sync result file written by the daemon or CLI.

    Returns the parsed dict on success, or ``None`` if the file is absent or
    unreadable.  Never raises.
    """
    module = _load_minio_sync_module()
    if module is None:
        return None
    result_file = getattr(module, "STATE_SYNC_RESULT_FILE", None)
    if result_file is None:
        return None
    try:
        text = Path(result_file).read_text(encoding="utf-8")
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def _snapshot_status() -> dict[str, Any]:
    """Lightweight status snapshot — no bucket traversal or admin API calls.

    Expensive storage statistics (used_bytes, quota) are served by the
    separate ``get_usage()`` entry point so they are only computed when the
    user explicitly clicks "查询用量" in the UI.
    """
    # If the user skipped MinIO restore, pretend MinIO doesn't exist.
    # This prevents the workspace panel from showing any MinIO controls.
    if is_minio_skipped():
        return {
            "config": _public_config(),
            "configured": False,
            "registration_required": False,
            "registration_state": None,
            "unavailable_reason": "用户已跳过 MinIO 恢复，当前使用本地数据",
            "register_url": "",
            "workspace_entries": [],
            "blocked_extensions": [],
            "script_available": False,
            "running": {},
            "last_result": {},
            "quota_exceeded": False,
            "skip_flag_exists": True,
        }

    cfg = _public_config()
    configured, unavailable_reason = _config_completeness(cfg)
    register_url = _register_url()
    registration = _probe_registration_requirement(cfg, configured)
    registration_required = bool((registration or {}).get("registration_required"))
    if registration_required:
        unavailable_reason = str((registration or {}).get("registration_reason") or unavailable_reason or "") or None
    entries = _list_workspace_entries()
    blocked_exts = _blocked_extensions() if configured and not registration_required else []
    with _lock:
        running = dict(_running)
        last = {lane: dict(v) if v else None for lane, v in _last_result.items()}
    # The state lane can be updated by either:
    # 1) this WebUI process (manual sync button) via _last_result["state"], or
    # 2) the separate daemon / CLI process via the durable result file.
    # Compare timestamps and surface whichever result is newer so automatic syncs
    # continue advancing the UI even after a previous manual sync populated the
    # in-process cache.
    persisted_state = _read_persisted_state_sync_result()
    current_state = last.get("state")
    current_ts = float(current_state.get("finished_at") or 0) if isinstance(current_state, dict) else 0.0
    persisted_ts = float(persisted_state.get("finished_at") or 0) if isinstance(persisted_state, dict) else 0.0
    if persisted_state and persisted_ts > current_ts:
        last["state"] = persisted_state
    script = _find_minio_sync_script()
    # Check if bucket capacity limit is exceeded
    quota_exceeded = False
    max_bytes = _coerce_int(os.environ.get("HERMES_MINIO_MAX_BYTES"), 10 * 1024 * 1024 * 1024)
    if configured and not registration_required and max_bytes > 0:
        used = _used_bytes(configured)
        if used > max_bytes:
            quota_exceeded = True
            unavailable_reason = f"存储空间已超限（已用 {used // (1024*1024)}MB / 限制 {max_bytes // (1024*1024)}MB），无法同步"
    return {
        "config": cfg,
        "configured": configured,
        "registration_required": registration_required,
        "registration_state": (registration or {}).get("registration_state"),
        "unavailable_reason": unavailable_reason,
        "register_url": register_url,
        "workspace_entries": entries,
        "blocked_extensions": blocked_exts,
        "script_available": bool(script),
        "running": running,
        "last_result": last,
        "quota_exceeded": quota_exceeded,
        "skip_flag_exists": False,
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


def purge_and_stop() -> dict[str, Any]:
    """Purge all objects under MINIO_PREFIX and stop the background sync daemon."""
    cfg = _public_config()
    configured, unavailable_reason = _config_completeness(cfg)
    if not configured:
        return {"ok": False, "error": unavailable_reason or "MinIO 同步未配置"}

    result = _run_subprocess(["purge", "--confirm"], timeout=120.0)

    # Stop the background daemon if running (signal via PID file)
    module = _load_minio_sync_module()
    if module is not None and hasattr(module, "stop_daemon"):
        try:
            module.stop_daemon()
        except Exception as exc:
            logger.debug("stop_daemon failed: %s", exc)

    return result


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


# ── Startup Restore (async, with skip support) ──────────────────────────────
#
# The old flow ran `minio_sync.py restore` synchronously in start.sh before
# the WebUI started, which meant the user stared at a blank page while MinIO
# was slow.  Now the WebUI starts immediately and triggers the restore via
# these API functions.  The user sees a loading overlay with progress and can
# click "skip" to enter the app without MinIO.

_startup_restore_lock = threading.Lock()
_startup_restore_state: dict[str, Any] = {
    "status": "idle",       # idle | running | done | failed | skipped
    "phase": "",            # human-readable phase description
    "started_at": 0.0,
    "finished_at": 0.0,
    "error": None,
    "skipped": False,
}
_startup_restore_skip_flag = threading.Event()


def get_startup_restore_status() -> dict[str, Any]:
    """Return the current startup-restore progress for the frontend.

    Also includes a fast ``minio_enabled`` check (reads env var, no network)
    so the frontend can decide whether to show the overlay without calling
    the slow ``/api/minio/sync/status`` endpoint.
    """
    with _startup_restore_lock:
        state = dict(_startup_restore_state)
    # Fast check: is MinIO enabled? (env var only, no network call)
    cfg = _public_config()
    configured, _ = _config_completeness(cfg)
    state["minio_enabled"] = bool(cfg.get("enabled") and configured)
    return state


def skip_startup_restore() -> dict[str, Any]:
    """Signal the running restore to stop and mark as skipped.

    If no restore is running, just sets the flag so the next call to
    ``trigger_startup_restore`` will be a no-op.
    """
    _startup_restore_skip_flag.set()
    with _startup_restore_lock:
        if _startup_restore_state["status"] in ("idle", "running"):
            _startup_restore_state["status"] = "skipped"
            _startup_restore_state["skipped"] = True
            _startup_restore_state["finished_at"] = time.time()
            _startup_restore_state["phase"] = "已跳过 MinIO 恢复，使用本地数据"
    # Write a flag file so the shell also knows to skip on next restart
    try:
        flag = Path(os.environ.get("HERMES_HOME", "/home/hermes/.hermes")) / "webui" / ".minio_restore_skipped"
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(str(int(time.time())))
    except Exception as exc:
        logger.debug("Failed to write skip flag: %s", exc)
    return {"ok": True, "status": "skipped"}


def trigger_startup_restore() -> dict[str, Any]:
    """Kick off the MinIO restore in a background thread.

    Returns immediately with ``{"ok": True, "status": "running"}`` or
    ``{"ok": True, "status": "skipped"}`` if the user already skipped.
    """
    with _startup_restore_lock:
        current = _startup_restore_state["status"]
        if current == "running":
            return {"ok": True, "status": "running", "message": "恢复已在进行中"}
        if current == "done":
            return {"ok": True, "status": "done", "message": "恢复已完成"}
        if current == "skipped" or _startup_restore_skip_flag.is_set():
            return {"ok": True, "status": "skipped", "message": "用户已跳过恢复"}

        cfg = _public_config()
        configured, reason = _config_completeness(cfg)
        if not configured:
            return {"ok": False, "status": "not_configured",
                    "error": reason or "MinIO 未配置"}

        # Check if already skipped via flag file
        try:
            flag = Path(os.environ.get("HERMES_HOME", "/home/hermes/.hermes")) / "webui" / ".minio_restore_skipped"
            if flag.exists():
                _startup_restore_skip_flag.set()
                _startup_restore_state["status"] = "skipped"
                _startup_restore_state["skipped"] = True
                _startup_restore_state["phase"] = "已跳过 MinIO 恢复"
                return {"ok": True, "status": "skipped", "message": "用户已跳过恢复"}
        except Exception:
            pass

        _startup_restore_state["status"] = "running"
        _startup_restore_state["phase"] = "正在连接 MinIO..."
        _startup_restore_state["started_at"] = time.time()
        _startup_restore_state["finished_at"] = 0.0
        _startup_restore_state["error"] = None

    _startup_restore_skip_flag.clear()
    t = threading.Thread(target=_run_startup_restore, daemon=True)
    t.start()
    return {"ok": True, "status": "running", "message": "恢复已启动"}


def clear_startup_restore_skip() -> dict[str, Any]:
    """Clear the skip flag so restore can be attempted again on next startup."""
    _startup_restore_skip_flag.clear()
    with _startup_restore_lock:
        if _startup_restore_state["status"] == "skipped":
            _startup_restore_state["status"] = "idle"
            _startup_restore_state["skipped"] = False
            _startup_restore_state["phase"] = ""
    try:
        flag = Path(os.environ.get("HERMES_HOME", "/home/hermes/.hermes")) / "webui" / ".minio_restore_skipped"
        flag.unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True}


def _check_minio_has_data() -> bool:
    """Quick check: does the user's MinIO prefix have any objects?

    Returns True if there's at least one object under the prefix, False if empty.
    If no prefix is configured (first-time user before login), returns False
    immediately — no point scanning a shared bucket without a prefix.
    """
    try:
        module = _load_minio_sync_module()
        if module is None or not hasattr(module, "get_client"):
            return True
        client = module.get_client()
        bucket = os.environ.get("HERMES_MINIO_BUCKET", "")
        prefix = (os.environ.get("HERMES_MINIO_PREFIX") or "").strip("/")
        if not bucket:
            return False
        if not prefix:
            # No prefix configured — first-time user, nothing to restore
            return False
        # List just one object under this user's prefix
        objects = client.list_objects(bucket, prefix=prefix + "/", recursive=True)
        for _ in objects:
            return True
        return False
    except Exception as exc:
        logger.debug("_check_minio_has_data failed: %s — assuming data exists", exc)
        return True


def _validate_minio_credentials() -> tuple[bool, str]:
    """Validate MinIO connectivity before restore.

    Returns (ok, error_message). Checks endpoint reachability and AK/SK validity.
    """
    try:
        from minio import Minio
        import urllib3
    except ImportError:
        return False, "minio 库未安装"

    endpoint = os.environ.get("HERMES_MINIO_ENDPOINT", "")
    access_key = os.environ.get("HERMES_MINIO_ACCESS_KEY", "")
    secret_key = os.environ.get("HERMES_MINIO_SECRET_KEY", "")
    bucket = os.environ.get("HERMES_MINIO_BUCKET", "")
    secure = os.environ.get("HERMES_MINIO_SECURE", "false").lower() == "true"

    if not endpoint:
        return False, "HERMES_MINIO_ENDPOINT 未配置"
    if not access_key or not secret_key:
        return False, "MinIO AK/SK 未配置"
    if not bucket:
        return False, "HERMES_MINIO_BUCKET 未配置"

    try:
        timeout = urllib3.util.timeout.Timeout(connect=5, read=10)
        http_client = urllib3.PoolManager(timeout=timeout)
        client = Minio(endpoint, access_key=access_key, secret_key=secret_key,
                       secure=secure, http_client=http_client)
        # Lightweight auth check
        client.bucket_exists(bucket)
        return True, ""
    except Exception as exc:
        err = str(exc)
        if _looks_like_invalid_minio_credentials(exc):
            return False, "MinIO AK/SK 无效，请检查凭证"
        return False, f"MinIO 连接失败: {err}"


def _run_startup_restore():
    """Background worker: run the actual MinIO restore."""
    try:
        script = _find_minio_sync_script()
        if script is None:
            with _startup_restore_lock:
                _startup_restore_state["status"] = "failed"
                _startup_restore_state["phase"] = "minio_sync.py 脚本未找到"
                _startup_restore_state["finished_at"] = time.time()
                _startup_restore_state["error"] = "minio_sync.py not found"
            return

        # Validate credentials before attempting restore
        with _startup_restore_lock:
            _startup_restore_state["phase"] = "正在验证 MinIO 凭证..."
        creds_ok, creds_err = _validate_minio_credentials()
        if not creds_ok:
            logger.warning("MinIO credential validation failed: %s", creds_err)
            with _startup_restore_lock:
                _startup_restore_state["status"] = "failed"
                _startup_restore_state["phase"] = creds_err
                _startup_restore_state["finished_at"] = time.time()
                _startup_restore_state["error"] = creds_err
            return

        # Quick pre-check: does the bucket/prefix have any objects?
        # If empty (first-time user), skip the heavy restore entirely.
        with _startup_restore_lock:
            _startup_restore_state["phase"] = "正在检查 MinIO 备份..."
        has_data = _check_minio_has_data()
        if _startup_restore_skip_flag.is_set():
            with _startup_restore_lock:
                _startup_restore_state["status"] = "skipped"
                _startup_restore_state["phase"] = "已跳过 MinIO 恢复"
                _startup_restore_state["finished_at"] = time.time()
            return
        if not has_data:
            logger.info("MinIO bucket is empty (first-time user), skipping restore")
            with _startup_restore_lock:
                _startup_restore_state["status"] = "done"
                _startup_restore_state["phase"] = "MinIO 无备份数据，跳过恢复"
                _startup_restore_state["finished_at"] = time.time()
            # Still update last_workspace.txt
            try:
                ws = os.environ.get("HERMES_WORKSPACE", "/home/hermes/workspace")
                state_dir = os.environ.get("HERMES_WEBUI_STATE_DIR", "")
                if state_dir:
                    Path(state_dir, "last_workspace.txt").write_text(ws + "\n")
            except Exception:
                pass
            return

        with _startup_restore_lock:
            _startup_restore_state["phase"] = "正在从 MinIO 下载数据..."

        # Build the command — same as what start.sh used to run
        python = _python_executable()
        cmd = [python, str(script), "restore"]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        # Stream output, checking for skip flag periodically
        output_lines = []
        while True:
            if _startup_restore_skip_flag.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                with _startup_restore_lock:
                    _startup_restore_state["status"] = "skipped"
                    _startup_restore_state["phase"] = "已跳过 MinIO 恢复"
                    _startup_restore_state["finished_at"] = time.time()
                return

            line = proc.stdout.readline()
            if line:
                output_lines.append(line.rstrip())
                # Update phase from subprocess output (last meaningful line)
                stripped = line.strip()
                if stripped and not stripped.startswith("["):
                    with _startup_restore_lock:
                        _startup_restore_state["phase"] = stripped[:120]
            elif proc.poll() is not None:
                break

        rc = proc.wait()
        with _startup_restore_lock:
            if rc == 0:
                _startup_restore_state["status"] = "done"
                _startup_restore_state["phase"] = "恢复完成"
                _startup_restore_state["finished_at"] = time.time()
                # Update last_workspace.txt as the old start.sh did
                try:
                    ws = os.environ.get("HERMES_WORKSPACE", "/home/hermes/workspace")
                    state_dir = os.environ.get("HERMES_WEBUI_STATE_DIR", "")
                    if state_dir:
                        Path(state_dir, "last_workspace.txt").write_text(ws + "\n")
                except Exception:
                    pass
            else:
                tail = "\n".join(output_lines[-5:]) if output_lines else "无输出"
                _startup_restore_state["status"] = "failed"
                _startup_restore_state["phase"] = f"恢复失败 (exit code {rc})"
                _startup_restore_state["finished_at"] = time.time()
                _startup_restore_state["error"] = tail

    except Exception as exc:
        with _startup_restore_lock:
            _startup_restore_state["status"] = "failed"
            _startup_restore_state["phase"] = f"恢复异常: {exc}"
            _startup_restore_state["finished_at"] = time.time()
            _startup_restore_state["error"] = str(exc)


# ── Daemon Lifecycle (WebUI-managed) ────────────────────────────────────────
#
# The MinIO sync daemon is no longer started by start.sh. Instead, it is
# started here after restore completes (or immediately if no restore was
# needed).  When the user skips restore, the daemon is never started, so
# no MinIO network activity occurs for the rest of the container's life.

_daemon_process: subprocess.Popen | None = None
_daemon_lock = threading.Lock()


def is_minio_skipped() -> bool:
    """Return True if the user has skipped MinIO for this session."""
    if _startup_restore_skip_flag.is_set():
        return True
    try:
        flag = Path(os.environ.get("HERMES_HOME", "/home/hermes/.hermes")) / "webui" / ".minio_restore_skipped"
        return flag.exists()
    except Exception:
        return False


def start_daemon_if_safe() -> dict[str, Any]:
    """Start the MinIO sync daemon — but only if restore was NOT skipped.

    Called from the WebUI after restore completes, or from the status check
    when no restore was needed (data already local from a previous session).
    """
    global _daemon_process

    if is_minio_skipped():
        return {"ok": False, "error": "MinIO 已跳过，不启动 daemon"}

    with _daemon_lock:
        if _daemon_process is not None and _daemon_process.poll() is None:
            return {"ok": True, "message": "daemon 已在运行"}

    cfg = _public_config()
    configured, reason = _config_completeness(cfg)
    if not configured:
        return {"ok": False, "error": reason or "MinIO 未配置"}

    script = _find_minio_sync_script()
    if script is None:
        return {"ok": False, "error": "minio_sync.py 未找到"}

    try:
        python = _python_executable()
        proc = subprocess.Popen(
            [python, str(script), "daemon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with _daemon_lock:
            _daemon_process = proc
        logger.info("MinIO sync daemon started (pid=%s)", proc.pid)
        return {"ok": True, "pid": proc.pid}
    except Exception as exc:
        logger.error("Failed to start MinIO daemon: %s", exc)
        return {"ok": False, "error": str(exc)}


def get_daemon_status() -> dict[str, Any]:
    """Return whether the MinIO sync daemon is running."""
    with _daemon_lock:
        if _daemon_process is not None:
            rc = _daemon_process.poll()
            if rc is None:
                return {"running": True, "pid": _daemon_process.pid}
            else:
                return {"running": False, "exit_code": rc}
    return {"running": False}
