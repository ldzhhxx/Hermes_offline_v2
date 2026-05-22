#!/usr/bin/env python3
"""MinIO state sync for Hermes Offline v2.

Provides restore-on-startup, periodic lightweight state sync, and explicit
manual workspace sync for k8s one-user-per-container usage. Uses the minio
Python SDK (S3-compatible). The ``mc`` binary is **not** required and is not
bundled in the image; mirror-style semantics are implemented in Python here.

Environment variables:
  HERMES_MINIO_ENABLED       - "true" to enable MinIO mode (default: disabled)
  HERMES_MINIO_ENDPOINT      - MinIO/S3 endpoint (e.g. minio.internal:9000)
  HERMES_MINIO_ACCESS_KEY    - Access key
  HERMES_MINIO_SECRET_KEY    - Secret key
  HERMES_MINIO_BUCKET        - Bucket name
  HERMES_MINIO_PREFIX        - Object prefix (e.g. user-liudezheng/diagent)
  HERMES_MINIO_SECURE        - "true" for HTTPS (default: "false")
  HERMES_MINIO_SYNC_INTERVAL - State sync interval in seconds (default: 300)
  HERMES_MINIO_BLOCKED_EXTENSIONS - Comma-separated file extensions to block
                               from workspace uploads (default: doc,docx,ppt,
                               pptx,xls,xlsx). Leading dots optional.

CLI:
  minio_sync.py restore
  minio_sync.py sync-state                       # state allowlist only
  minio_sync.py sync-workspace [--mode safe|mirror] [--cleanup-remote] \
                               [--paths a/b c.txt ...]
  minio_sync.py daemon                           # periodic state sync only
  minio_sync.py sync                             # legacy alias for sync-state
"""

import argparse
import hashlib
import json
import logging
import os
import signal
import sqlite3
import subprocess  # noqa: F401  (kept for forward-compat hooks)
import sys
import tempfile
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="[minio-sync] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("minio-sync")

# ── Configuration ───────────────────────────────────────────────────────────

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/home/hermes/.hermes"))
HERMES_WORKSPACE = Path(os.environ.get("HERMES_WORKSPACE", "/home/hermes/workspace"))

MINIO_ENDPOINT = os.environ.get("HERMES_MINIO_ENDPOINT", "")
MINIO_ACCESS_KEY = os.environ.get("HERMES_MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.environ.get("HERMES_MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.environ.get("HERMES_MINIO_BUCKET", "")
MINIO_PREFIX = os.environ.get("HERMES_MINIO_PREFIX", "").strip("/")
MINIO_SECURE = os.environ.get("HERMES_MINIO_SECURE", "false").lower() == "true"
SYNC_INTERVAL = int(os.environ.get("HERMES_MINIO_SYNC_INTERVAL", "300"))

# Files/dirs to sync under HERMES_HOME (relative paths).
# These are lightweight Hermes "state" — synced both periodically and on demand.
# Workspace contents are *not* in this list and are only ever uploaded via the
# explicit `sync-workspace` command.
SYNC_INCLUDES_HOME = [
    "skills",
    "state.db",
    "kanban.db",
    "response_store.db",
    "SOUL.md",
    "gateway_state.json",
    "channel_directory.json",
    "platforms",
    "sessions",
    "memories",
    "cron",
    "sandboxes",
    "webui/models_cache.json",
    "webui/sessions",
]

TARGET_UID = int(os.environ.get("HERMES_RUNTIME_UID", str(os.getuid())))
TARGET_GID = int(os.environ.get("HERMES_RUNTIME_GID", str(os.getgid())))

# Sensitive files to NEVER sync
SENSITIVE_PATTERNS = {
    ".env",
    "auth.json",
    "config.yaml",
    "auth.lock",
    "gateway.pid",
    "gateway.lock",
    "webui/settings.json",
    "webui/.sessions.json",
}

# Workspace sync modes
WORKSPACE_MODE_SAFE = "safe"      # incremental: skip files that match remote
WORKSPACE_MODE_MIRROR = "mirror"  # always overwrite remote with local
WORKSPACE_MODES = (WORKSPACE_MODE_SAFE, WORKSPACE_MODE_MIRROR)

# Blocked file extensions for workspace uploads (configurable via env).
# Comma-separated, case-insensitive, leading dots optional.
_DEFAULT_BLOCKED_EXTENSIONS = "doc,docx,ppt,pptx,xls,xlsx"


def _parse_blocked_extensions(raw: str) -> frozenset[str]:
    """Parse a comma-separated extension list into a normalized frozenset.

    Accepts values with or without leading dots, trims whitespace, lowercases.
    Returns extensions *without* leading dots for comparison.
    """
    exts: set[str] = set()
    for part in raw.split(","):
        part = part.strip().lower().lstrip(".")
        if part:
            exts.add(part)
    return frozenset(exts)


def get_blocked_extensions() -> frozenset[str]:
    """Return the effective set of blocked workspace upload extensions."""
    raw = os.environ.get("HERMES_MINIO_BLOCKED_EXTENSIONS", _DEFAULT_BLOCKED_EXTENSIONS)
    return _parse_blocked_extensions(raw)


def is_blocked_extension(filename: str) -> bool:
    """Return True if the file's extension is in the blocked set."""
    blocked = get_blocked_extensions()
    if not blocked:
        return False
    ext = Path(filename).suffix.lower().lstrip(".")
    return ext in blocked


def is_sensitive(rel_path: str) -> bool:
    """Check if a relative path matches sensitive patterns."""
    for pat in SENSITIVE_PATTERNS:
        if rel_path == pat or rel_path.endswith("/" + pat):
            return True
    # Skip WAL/SHM files - we handle SQLite via backup API
    if rel_path.endswith(("-wal", "-shm", "-journal")):
        return True
    return False


def get_client():
    """Create MinIO client."""
    from minio import Minio

    return Minio(
        MINIO_ENDPOINT,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=MINIO_SECURE,
    )


def object_key(rel_path: str) -> str:
    """Build full object key with prefix."""
    if MINIO_PREFIX:
        return f"{MINIO_PREFIX}/{rel_path}"
    return rel_path


def is_allowed_home_path(rel_path: str) -> bool:
    """Return True when a restored HERMES_HOME path is in the sync allowlist."""
    return any(rel_path == item or rel_path.startswith(item + "/") for item in SYNC_INCLUDES_HOME)


def _chown_path(path: Path) -> None:
    """Best-effort ownership repair for restored paths."""
    try:
        os.chown(path, TARGET_UID, TARGET_GID)
    except OSError:
        pass


def _chown_parent_chain(path: Path, stop_at: Path) -> None:
    """Best-effort chown for a restored path and its parents under stop_at."""
    stop_at = stop_at.resolve()
    current = path.resolve()
    while True:
        _chown_path(current)
        if current == stop_at:
            break
        try:
            current.relative_to(stop_at)
        except ValueError:
            break
        if current.parent == current:
            break
        current = current.parent


def _download_object_atomically(client, bucket: str, object_name: str, dest: Path) -> None:
    """Download to a temp file in the target dir, then atomically replace."""
    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=".minio_restore_")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        client.fget_object(bucket, object_name, str(tmp_path))
        os.replace(tmp_path, dest)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def safe_sqlite_backup(db_path: Path, dest_path: Path):
    """Create a consistent SQLite backup using the backup API."""
    if not db_path.exists():
        return
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(dest_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _md5_of_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Compute md5 hex digest of a file, streaming chunks."""
    h = hashlib.md5()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _remote_object_meta(client, bucket: str, object_name: str):
    """Return (size, etag) for a remote object, or None if it does not exist.

    The MinIO Python SDK exposes a number of S3-style errors but stat_object
    consistently raises an error whose attribute ``code`` includes
    ``NoSuchKey`` when the object does not exist. We use a coarse, defensive
    check so this also works under the test fakes.
    """
    try:
        info = client.stat_object(bucket, object_name)
    except Exception as exc:  # pragma: no cover - exercised via tests with fake client
        code = getattr(exc, "code", "") or ""
        msg = str(exc)
        if "NoSuchKey" in code or "NoSuchKey" in msg or "Not Found" in msg or "404" in msg:
            return None
        # Treat unknown errors as "missing" so we err on the side of uploading
        # rather than silently skipping. The actual upload will surface any
        # real connectivity issue.
        log.debug("stat_object(%s) raised %s; treating as missing", object_name, exc)
        return None
    size = getattr(info, "size", None)
    etag = getattr(info, "etag", "") or ""
    if isinstance(etag, str):
        etag = etag.strip('"')
    return size, etag


def _should_skip_safe_upload(client, object_name: str, local_path: Path) -> bool:
    """Return True if the remote already has an identical copy.

    For multipart uploads, S3 etag is not a plain MD5. In that case we fall
    back to size-based equality. This intentionally errs on the side of
    "upload again" rather than skipping a real change.
    """
    meta = _remote_object_meta(client, MINIO_BUCKET, object_name)
    if meta is None:
        return False
    size, etag = meta
    try:
        local_size = local_path.stat().st_size
    except OSError:
        return False
    if size != local_size:
        return False
    # If etag looks like a plain hex md5 (no '-'), compare to local md5.
    if etag and "-" not in etag and len(etag) == 32:
        try:
            return _md5_of_file(local_path) == etag.lower()
        except OSError:
            return False
    # Multipart or unknown etag shape: trust size equality only.
    return True


# ── Upload: state (lightweight, daemon + manual) ───────────────────────────


def sync_state_to_minio() -> dict:
    """Upload only Hermes state (allowlist) to MinIO. Never touches workspace.

    Returns a result dict: {"uploaded": int, "errors": [str], "mode": "state"}.
    """
    client = get_client()

    if not client.bucket_exists(MINIO_BUCKET):
        client.make_bucket(MINIO_BUCKET)

    uploaded = 0
    errors: list[str] = []

    sqlite_dbs = ["state.db", "kanban.db", "response_store.db"]
    with tempfile.TemporaryDirectory(prefix="hermes_sync_") as tmpdir:
        for db_name in sqlite_dbs:
            db_path = HERMES_HOME / db_name
            if db_path.exists():
                backup_path = Path(tmpdir) / db_name
                try:
                    safe_sqlite_backup(db_path, backup_path)
                    client.fput_object(
                        MINIO_BUCKET, object_key(f"home/{db_name}"), str(backup_path)
                    )
                    uploaded += 1
                except Exception as e:
                    log.warning("Failed to backup/upload %s: %s", db_name, e)
                    errors.append(f"{db_name}: {e}")

        for item in SYNC_INCLUDES_HOME:
            if item in sqlite_dbs:
                continue
            full_path = HERMES_HOME / item
            if not full_path.exists():
                continue
            if full_path.is_file():
                if is_sensitive(item):
                    continue
                try:
                    client.fput_object(
                        MINIO_BUCKET, object_key(f"home/{item}"), str(full_path)
                    )
                    uploaded += 1
                except Exception as e:
                    log.warning("Failed to upload %s: %s", item, e)
                    errors.append(f"{item}: {e}")
            elif full_path.is_dir():
                for fpath in full_path.rglob("*"):
                    if not fpath.is_file():
                        continue
                    rel = str(fpath.relative_to(HERMES_HOME))
                    if is_sensitive(rel):
                        continue
                    try:
                        client.fput_object(
                            MINIO_BUCKET, object_key(f"home/{rel}"), str(fpath)
                        )
                        uploaded += 1
                    except Exception as e:
                        log.warning("Failed to upload %s: %s", rel, e)
                        errors.append(f"{rel}: {e}")

    log.info("State sync to MinIO complete: %d objects uploaded.", uploaded)
    return {"mode": "state", "uploaded": uploaded, "errors": errors}


# ── Workspace path validation + entry listing ──────────────────────────────


def _normalize_workspace_rel(rel: str) -> str:
    """Strip leading slashes / dots so callers can pass either './foo' or 'foo'."""
    if not isinstance(rel, str):
        raise ValueError("path must be a string")
    candidate = rel.strip()
    if not candidate:
        raise ValueError("path is empty")
    # Remove leading `./` so `./foo` and `foo` are equivalent.
    while candidate.startswith("./"):
        candidate = candidate[2:]
    if candidate in ("", "."):
        raise ValueError("path is empty")
    if candidate.startswith("/"):
        raise ValueError(f"absolute path not allowed: {rel!r}")
    if ".." in Path(candidate).parts:
        raise ValueError(f"path traversal not allowed: {rel!r}")
    return candidate


def validate_workspace_paths(raw_paths) -> list[str]:
    """Return a sanitized list of relative workspace paths.

    Rules:
      * each entry must be a non-empty string
      * absolute paths and `..` traversal are rejected
      * the resolved path must remain under ``HERMES_WORKSPACE``
      * the path must currently exist locally (otherwise nothing to upload)

    Symlinks pointing outside the workspace are rejected because a
    user-supplied selection should not be able to leak files from outside the
    container's workspace boundary, even if the link technically resolves to
    a real file.
    """
    if raw_paths is None:
        return []
    if not isinstance(raw_paths, (list, tuple)):
        raise ValueError("paths must be a list of strings")
    workspace_root = HERMES_WORKSPACE.resolve()
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_paths:
        rel = _normalize_workspace_rel(raw)
        candidate = (HERMES_WORKSPACE / rel).resolve()
        try:
            candidate.relative_to(workspace_root)
        except ValueError:
            raise ValueError(f"path escapes workspace: {raw!r}")
        if not candidate.exists():
            raise ValueError(f"path does not exist: {rel}")
        # Re-derive the canonical relative form from the resolved path so the
        # returned list is normalized regardless of the user's input shape.
        canonical = str(candidate.relative_to(workspace_root))
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append(canonical)
    return out


def _iter_workspace_files_under(rel_path: str):
    """Yield (rel, absolute_path) for files reachable from ``rel_path``.

    ``rel_path`` is a workspace-relative entry (file or directory) that has
    already passed validation. The yielded ``rel`` is the workspace-relative
    string for ``fpath`` so callers can construct ``workspace/{rel}`` keys.
    """
    base = (HERMES_WORKSPACE / rel_path).resolve()
    workspace_root = HERMES_WORKSPACE.resolve()
    if base.is_file():
        yield rel_path, base
        return
    if not base.is_dir():
        return
    for fpath in base.rglob("*"):
        if not fpath.is_file():
            continue
        try:
            rel = str(fpath.resolve().relative_to(workspace_root))
        except ValueError:
            # Skip anything that resolved outside the workspace (shouldn't
            # happen because base passed validation, but defensive).
            continue
        yield rel, fpath


def _dir_size_and_count(path: Path, max_files: int = 50000) -> tuple[int, int]:
    """Return (total_size_bytes, file_count) under *path*.

    Caps the walk at ``max_files`` files to avoid pathological cases (huge
    workspaces, symlink loops). The cap is generous; real Hermes workspaces
    rarely approach it.
    """
    total = 0
    count = 0
    if not path.is_dir():
        return 0, 0
    for fpath in path.rglob("*"):
        if count >= max_files:
            break
        try:
            if fpath.is_file():
                total += fpath.stat().st_size
                count += 1
        except OSError:
            continue
    return total, count


def list_workspace_entries(max_entries: int = 200) -> list[dict]:
    """List top-level workspace entries with size + child counts.

    Returns at most ``max_entries`` entries (sorted: directories first, then
    files; alphabetical within each group). Each entry has::

        {"path": "<relative>", "type": "file"|"dir", "size": <bytes>,
         "child_count": <int>}  # child_count present for directories
    """
    if not HERMES_WORKSPACE.exists() or not HERMES_WORKSPACE.is_dir():
        return []
    entries: list[dict] = []
    try:
        children = sorted(HERMES_WORKSPACE.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    for child in children:
        if len(entries) >= max_entries:
            break
        # Skip hidden entries by default — they're rarely user-relevant for
        # sync, and including dotfiles can leak `.git/`, `.cache/`, etc.
        if child.name.startswith("."):
            continue
        try:
            if child.is_file():
                size = child.stat().st_size
                entries.append({
                    "path": child.name,
                    "type": "file",
                    "size": int(size),
                })
            elif child.is_dir():
                size, count = _dir_size_and_count(child)
                entries.append({
                    "path": child.name,
                    "type": "dir",
                    "size": int(size),
                    "child_count": int(count),
                })
        except OSError:
            continue
    entries.sort(key=lambda e: (e["type"] != "dir", e["path"].lower()))
    return entries


def compute_prefix_used_bytes(client=None, max_objects: int = 500000) -> int:
    """Return total bytes stored under the configured ``MINIO_PREFIX``.

    Iterates ``client.list_objects`` and sums ``size``. Returns ``0`` if the
    bucket is unreachable or the listing fails — callers treat that as
    "unknown" rather than failing the whole status request.
    """
    if not MINIO_BUCKET:
        return 0
    try:
        if client is None:
            client = get_client()
    except Exception:  # pragma: no cover - depends on minio package
        return 0
    prefix = object_key("")
    total = 0
    seen = 0
    try:
        for obj in client.list_objects(MINIO_BUCKET, prefix=prefix, recursive=True):
            seen += 1
            if seen > max_objects:
                break
            size = getattr(obj, "size", None)
            if isinstance(size, int) and size >= 0:
                total += size
    except Exception as exc:  # pragma: no cover - depends on minio errors
        log.debug("compute_prefix_used_bytes failed: %s", exc)
        return 0
    return int(total)


# ── Quota discovery ────────────────────────────────────────────────────────

# Bucket-tag conventions checked when neither the admin API nor a per-prefix
# tag is available. The first hit wins. ``hermes-quota-bytes-<prefix>`` lets
# operators give different prefixes (one-user-per-container) different quotas
# from a single bucket without enabling the admin API. The unsuffixed key
# applies to every prefix in the bucket.
_QUOTA_TAG_PREFIX_KEY = "hermes-quota-bytes"  # base name; prefixed with "<prefix>:"


def _quota_env_bytes() -> int:
    """Read the operator-configured override quota in bytes (``0`` = unset).

    Kept separate from :func:`discover_quota` so callers (and tests) can
    isolate the env-override path from real service-derived discovery.
    """
    raw = os.environ.get("HERMES_MINIO_QUOTA_BYTES", "")
    try:
        value = int(str(raw).strip()) if raw else 0
    except ValueError:
        return 0
    return value if value > 0 else 0


def _quota_from_admin_api(client=None) -> int:
    """Try the MinIO admin API for a bucket-level quota.

    Returns 0 when the SDK cannot import :class:`MinioAdmin`, when admin
    credentials are missing/insufficient, or when the bucket has no quota
    configured. Never raises; failures fall through to the next discovery
    layer so unconfigured deployments stay quiet in logs.
    """
    if not MINIO_BUCKET or not MINIO_ENDPOINT:
        return 0
    try:
        from minio import MinioAdmin  # type: ignore
    except Exception:  # pragma: no cover - older SDK or missing module
        return 0
    try:
        admin = MinioAdmin(
            MINIO_ENDPOINT,
            credentials=None,  # built from MINIO_ACCESS_KEY/SECRET_KEY env vars
            secure=MINIO_SECURE,
        )
    except Exception:  # pragma: no cover - depends on SDK version
        # Construction signatures differ across SDK versions; treat as no-op.
        return 0
    fetcher = getattr(admin, "get_bucket_quota", None) or getattr(
        admin, "bucket_quota_get", None
    )
    if fetcher is None:
        return 0
    try:
        info = fetcher(MINIO_BUCKET)
    except Exception as exc:  # pragma: no cover - depends on remote
        log.debug("bucket_quota admin call failed: %s", exc)
        return 0
    quota = 0
    if isinstance(info, dict):
        quota = int(info.get("quota") or info.get("size") or 0)
    else:
        quota = int(getattr(info, "quota", 0) or 0)
    return quota if quota > 0 else 0


def _quota_from_bucket_tag(client=None) -> int:
    """Try a bucket-tag convention, scoped to the configured prefix when set.

    Two keys are considered:

      * ``hermes-quota-bytes-<prefix>`` — per-prefix override (preferred when
        many users share the same bucket via different ``HERMES_MINIO_PREFIX``
        values).
      * ``hermes-quota-bytes`` — bucket-wide default.

    The value must be a positive base-10 integer; anything else is treated
    as "no quota configured" and falls through to the next discovery layer.
    """
    if not MINIO_BUCKET:
        return 0
    try:
        if client is None:
            client = get_client()
    except Exception:  # pragma: no cover - depends on minio package
        return 0
    fetcher = getattr(client, "get_bucket_tagging", None)
    if fetcher is None:
        return 0
    try:
        tags = fetcher(MINIO_BUCKET)
    except Exception as exc:  # pragma: no cover - depends on remote/SDK
        log.debug("get_bucket_tagging failed: %s", exc)
        return 0
    # The SDK can return a Tags-like object exposing __iter__/items, or a
    # plain dict. Normalize both shapes to a plain mapping.
    if tags is None:
        return 0
    items: dict[str, str] = {}
    if hasattr(tags, "items"):
        try:
            for k, v in tags.items():
                items[str(k)] = str(v)
        except Exception:  # pragma: no cover - defensive
            return 0
    elif isinstance(tags, dict):
        items = {str(k): str(v) for k, v in tags.items()}
    else:
        return 0
    keys_in_order: list[str] = []
    if MINIO_PREFIX:
        keys_in_order.append(f"{_QUOTA_TAG_PREFIX_KEY}-{MINIO_PREFIX}")
    keys_in_order.append(_QUOTA_TAG_PREFIX_KEY)
    for key in keys_in_order:
        raw = items.get(key)
        if not raw:
            continue
        try:
            value = int(str(raw).strip())
        except ValueError:
            continue
        if value > 0:
            return value
    return 0


def discover_quota(client=None) -> tuple[int, str]:
    """Discover the per-bucket / per-prefix storage quota in bytes.

    Resolution order:

      1. MinIO admin API (``bucket_quota_get``) — service-derived, exact.
      2. S3 bucket tag ``hermes-quota-bytes-<prefix>`` then
         ``hermes-quota-bytes`` — operator can set this from ``mc tag set``
         without enabling the admin API.
      3. ``HERMES_MINIO_QUOTA_BYTES`` env override — kept as an explicit
         fallback for deployments that cannot expose either of the above.
      4. Otherwise unset (``0``, ``"unset"``).

    Returns ``(bytes, source)``. ``source`` is one of ``'admin_api'``,
    ``'bucket_tag'``, ``'env'``, or ``'unset'``.

    The bridge surfaces ``source`` to the WebUI so operators understand
    *why* a particular number is shown — and that the env override is the
    last-resort path, not the canonical one.
    """
    quota = _quota_from_admin_api(client)
    if quota > 0:
        return quota, "admin_api"
    quota = _quota_from_bucket_tag(client)
    if quota > 0:
        return quota, "bucket_tag"
    quota = _quota_env_bytes()
    if quota > 0:
        return quota, "env"
    return 0, "unset"


# ── Upload: workspace (manual only) ────────────────────────────────────────


def sync_workspace_to_minio(mode: str = WORKSPACE_MODE_SAFE,
                            cleanup_remote: bool = False,
                            paths: list[str] | None = None) -> dict:
    """Upload the workspace tree to MinIO with explicit semantics.

    ``mode``:
      - ``safe`` (default) — incremental: skip files whose remote copy already
        matches the local size/etag. Never deletes anything remotely.
      - ``mirror`` — overwrite remote with local for every file.

    ``cleanup_remote``: only honored when ``mode='mirror'``. When True, deletes
    remote objects under ``workspace/`` that no longer exist locally. This is
    *opt-in* and labeled as dangerous in the WebUI — never the default.

    ``paths``: optional list of workspace-relative entries to sync. When
    provided, only files reached from those entries are uploaded, and the
    cleanup step (if enabled) only considers remote objects whose key starts
    with one of the corresponding prefixes — never the entire ``workspace/``
    tree. ``None`` or an empty list means "sync the full workspace" (legacy
    behaviour, kept for the CLI/daemon path that pre-dates path selection).
    """
    if mode not in WORKSPACE_MODES:
        raise ValueError(f"invalid workspace sync mode: {mode!r}")
    if cleanup_remote and mode != WORKSPACE_MODE_MIRROR:
        raise ValueError("cleanup_remote requires mode='mirror'")

    selected = validate_workspace_paths(paths) if paths else []

    client = get_client()

    if not client.bucket_exists(MINIO_BUCKET):
        client.make_bucket(MINIO_BUCKET)

    uploaded = 0
    skipped = 0
    blocked = 0
    blocked_details: list[str] = []
    deleted = 0
    errors: list[str] = []
    local_keys: set[str] = set()

    if HERMES_WORKSPACE.exists():
        if selected:
            file_iter = (
                pair
                for entry in selected
                for pair in _iter_workspace_files_under(entry)
            )
        else:
            file_iter = (
                (str(fpath.relative_to(HERMES_WORKSPACE)), fpath)
                for fpath in HERMES_WORKSPACE.rglob("*")
                if fpath.is_file()
            )
        for rel, fpath in file_iter:
            if is_blocked_extension(rel):
                blocked += 1
                blocked_details.append(rel)
                log.debug("Blocked by extension filter: %s", rel)
                continue
            obj = object_key(f"workspace/{rel}")
            local_keys.add(obj)
            if mode == WORKSPACE_MODE_SAFE:
                try:
                    if _should_skip_safe_upload(client, obj, fpath):
                        skipped += 1
                        continue
                except Exception as e:
                    log.debug("safe-mode pre-check failed for %s: %s", rel, e)
            try:
                client.fput_object(MINIO_BUCKET, obj, str(fpath))
                uploaded += 1
            except Exception as e:
                log.warning("Failed to upload workspace/%s: %s", rel, e)
                errors.append(f"workspace/{rel}: {e}")

    if cleanup_remote and mode == WORKSPACE_MODE_MIRROR:
        # When the caller restricted the sync to a subset of the workspace,
        # only consider remote objects under those subtrees. Without this
        # scoping, a "mirror+cleanup" of one subdirectory would nuke every
        # other remote workspace file the user *didn't* select. That is the
        # exact safety footgun the v2 plan calls out:
        #   "cleanup/remove mode must only consider the selected scope, not
        #    nuke unrelated remote objects".
        #
        # File entries pass an exact key + `exact=True` because a plain
        # prefix match on `workspace/notes.txt` would also match siblings
        # like `workspace/notes.txt.bak`. Directory entries use a trailing
        # slash for the same reason — `workspace/foo/` cannot accidentally
        # match `workspace/foo_other/`.
        cleanup_targets: list[tuple[str, bool]] = []
        if selected:
            for entry in selected:
                abs_entry = (HERMES_WORKSPACE / entry)
                if abs_entry.is_dir():
                    cleanup_targets.append((object_key(f"workspace/{entry}/"), False))
                else:
                    cleanup_targets.append((object_key(f"workspace/{entry}"), True))
        else:
            cleanup_targets.append((object_key("workspace/"), False))
        seen_remote: set[str] = set()
        for ws_prefix, exact in cleanup_targets:
            try:
                if exact:
                    # Single-key check: list with the exact name as prefix and
                    # only act on the matching object.
                    remote_objects = [
                        obj for obj in client.list_objects(
                            MINIO_BUCKET, prefix=ws_prefix, recursive=False)
                        if getattr(obj, "object_name", None) == ws_prefix
                    ]
                else:
                    remote_objects = list(client.list_objects(
                        MINIO_BUCKET, prefix=ws_prefix, recursive=True))
            except Exception as e:
                log.warning("Failed to list remote workspace prefix %s: %s",
                            ws_prefix, e)
                errors.append(f"cleanup-list {ws_prefix}: {e}")
                continue
            for obj in remote_objects:
                name = getattr(obj, "object_name", None)
                if not name or name in seen_remote or name in local_keys:
                    continue
                seen_remote.add(name)
                try:
                    client.remove_object(MINIO_BUCKET, name)
                    deleted += 1
                except Exception as e:
                    log.warning("Failed to remove remote %s: %s", name, e)
                    errors.append(f"remove {name}: {e}")

    log.info(
        "Workspace sync (mode=%s, cleanup_remote=%s, selected=%d) complete: "
        "uploaded=%d skipped=%d blocked=%d deleted=%d errors=%d",
        mode, cleanup_remote, len(selected),
        uploaded, skipped, blocked, deleted, len(errors),
    )
    return {
        "mode": "workspace",
        "workspace_mode": mode,
        "cleanup_remote": cleanup_remote,
        "selected_paths": list(selected),
        "uploaded": uploaded,
        "skipped": skipped,
        "blocked": blocked,
        "blocked_details": blocked_details,
        "deleted": deleted,
        "errors": errors,
    }


# ── Backward-compat shim ───────────────────────────────────────────────────

def sync_to_minio() -> dict:
    """Legacy entry point. State-only. Workspace must be triggered explicitly."""
    return sync_state_to_minio()


# ── Download (Restore from MinIO) ──────────────────────────────────────────


def restore_from_minio():
    """Download state from MinIO to local paths."""
    client = get_client()

    if not client.bucket_exists(MINIO_BUCKET):
        log.info("Bucket %s does not exist; skipping restore.", MINIO_BUCKET)
        return False

    prefix = object_key("")
    objects = list(client.list_objects(MINIO_BUCKET, prefix=prefix, recursive=True))
    if not objects:
        log.info("No backup content found at %s/%s; skipping restore.", MINIO_BUCKET, prefix)
        return False

    restored = 0
    for obj in objects:
        # Strip the prefix to get relative path
        rel = obj.object_name
        if MINIO_PREFIX:
            rel = rel[len(MINIO_PREFIX) + 1 :]

        if rel.startswith("home/"):
            local_rel = rel[5:]  # strip "home/"
            if is_sensitive(local_rel) or not is_allowed_home_path(local_rel):
                continue
            dest = HERMES_HOME / local_rel
            stop_at = HERMES_HOME
        elif rel.startswith("workspace/"):
            local_rel = rel[10:]  # strip "workspace/"
            dest = HERMES_WORKSPACE / local_rel
            stop_at = HERMES_WORKSPACE
        else:
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        _chown_parent_chain(dest.parent, stop_at)
        try:
            _download_object_atomically(client, MINIO_BUCKET, obj.object_name, dest)
            _chown_path(dest)
            restored += 1
        except Exception as e:
            log.warning("Failed to restore %s: %s", obj.object_name, e)

    log.info("Restore from MinIO complete: %d objects restored.", restored)
    return restored > 0


# ── Daemon mode ─────────────────────────────────────────────────────────────

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True


def run_daemon():
    """Run periodic state-only sync loop.

    Workspace contents are *never* synced from this loop. Workspace upload is
    a manual, user-triggered action so first-sync against a populated
    workspace cannot overload MinIO in production clusters.
    """
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("Periodic state sync daemon started (interval=%ds; workspace excluded).", SYNC_INTERVAL)
    while not _shutdown:
        time.sleep(SYNC_INTERVAL)
        if _shutdown:
            break
        try:
            sync_state_to_minio()
        except Exception as e:
            log.error("State sync failed: %s", e)

    # Final sync on shutdown
    log.info("Performing final state sync before exit...")
    try:
        sync_state_to_minio()
    except Exception as e:
        log.error("Final state sync failed: %s", e)
    log.info("Daemon exiting.")


# ── CLI ─────────────────────────────────────────────────────────────────────


def _emit_result(result: dict) -> None:
    """Print a one-line JSON summary so callers (WebUI subprocess) can parse it."""
    try:
        print("RESULT_JSON " + json.dumps(result, ensure_ascii=False), flush=True)
    except Exception:
        # Never let a print failure mask the actual sync result.
        pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minio_sync.py")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("restore", help="restore Hermes state from MinIO")
    sub.add_parser("sync-state", help="upload Hermes state allowlist to MinIO")
    sub.add_parser("sync", help="alias for sync-state (legacy)")
    sub.add_parser("daemon", help="run periodic state sync (workspace excluded)")

    ws = sub.add_parser("sync-workspace", help="upload workspace tree to MinIO")
    ws.add_argument(
        "--mode",
        choices=WORKSPACE_MODES,
        default=WORKSPACE_MODE_SAFE,
        help="safe (default, incremental) or mirror (always overwrite)",
    )
    ws.add_argument(
        "--cleanup-remote",
        action="store_true",
        help="when --mode=mirror, delete remote files that no longer exist locally",
    )
    ws.add_argument(
        "--paths",
        nargs="*",
        default=None,
        metavar="REL_PATH",
        help="optional workspace-relative entries to sync (files or dirs); "
             "when omitted, the entire workspace is synced",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.cmd:
        parser.print_usage()
        return 1

    if args.cmd == "restore":
        ok = restore_from_minio()
        _emit_result({"mode": "restore", "ok": bool(ok)})
        return 0
    if args.cmd in ("sync-state", "sync"):
        result = sync_state_to_minio()
        _emit_result(result)
        return 0
    if args.cmd == "sync-workspace":
        try:
            result = sync_workspace_to_minio(
                mode=args.mode,
                cleanup_remote=bool(args.cleanup_remote),
                paths=args.paths,
            )
        except ValueError as e:
            log.error("%s", e)
            _emit_result({"mode": "workspace", "error": str(e)})
            return 2
        _emit_result(result)
        return 0
    if args.cmd == "daemon":
        run_daemon()
        return 0

    parser.print_usage()
    return 1


if __name__ == "__main__":
    sys.exit(main())
