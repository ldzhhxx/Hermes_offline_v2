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
import fcntl
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
    "logs",
    "sandboxes",
    "webui/models_cache.json",
    "webui/sessions",
]

TARGET_UID = int(os.environ.get("HERMES_RUNTIME_UID", str(os.getuid())))
TARGET_GID = int(os.environ.get("HERMES_RUNTIME_GID", str(os.getgid())))

# Durable file written after every state sync so the WebUI bridge (a separate
# process) can read the latest auto-sync result without needing shared memory.
STATE_SYNC_RESULT_FILE = HERMES_HOME / ".minio_state_sync_last.json"

# Cross-process lock file to prevent concurrent state syncs between the daemon
# and WebUI-triggered subprocess invocations.
_STATE_SYNC_LOCK_FILE = HERMES_HOME / ".minio_state_sync.lock"


class _SyncLock:
    """Non-blocking file lock for cross-process mutual exclusion.

    Uses fcntl.flock(LOCK_EX | LOCK_NB) so a second process attempting to
    sync concurrently will fail immediately rather than queue up stale syncs.
    """

    def __init__(self, lock_path: Path):
        self._path = lock_path
        self._fd: int | None = None

    def __enter__(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            os.close(self._fd)
            self._fd = None
            raise
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self._fd)
            self._fd = None

# 精确路径白名单 - 只过滤这些确切位置的文件，不误杀 skills/ 下的同名文件
SENSITIVE_PATHS = {
    '.env',
    'auth.json',
    'config.yaml',
    'auth.lock',
    'gateway.pid',
    'gateway.lock',
    'webui/settings.json',
    'webui/.sessions.json',
}

# Keep old name as alias for backward compat with any external references
SENSITIVE_PATTERNS = SENSITIVE_PATHS

# Workspace sync modes
WORKSPACE_MODE_SAFE = "safe"      # incremental: skip files that match remote
WORKSPACE_MODE_MIRROR = "mirror"  # always overwrite remote with local
WORKSPACE_MODES = (WORKSPACE_MODE_SAFE, WORKSPACE_MODE_MIRROR)

# Blocked file extensions for workspace uploads (configurable via env).
# Comma-separated, case-insensitive, leading dots optional.
_DEFAULT_BLOCKED_EXTENSIONS = "doc,docx,ppt,pptx,xls,xlsx"

# Bucket capacity limit (bytes). Sync is refused when used > max.
MINIO_MAX_BYTES = int(os.environ.get("HERMES_MINIO_MAX_BYTES", str(10 * 1024 * 1024 * 1024)))


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
    """只过滤精确路径的敏感文件，不误杀 skills/ 下的同名文件。"""
    if rel_path in SENSITIVE_PATHS:
        return True
    if rel_path.endswith(('-wal', '-shm', '-journal')):
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


def _configure_mc_alias() -> str:
    """配置 mc alias 用于后续 mc 命令，返回 alias 名称。"""
    alias = 'hermes-minio'
    scheme = 'https' if MINIO_SECURE else 'http'
    endpoint = f'{scheme}://{MINIO_ENDPOINT}'
    result = subprocess.run(
        ['mc', 'alias', 'set', alias, endpoint, MINIO_ACCESS_KEY, MINIO_SECRET_KEY],
        capture_output=True, text=True, check=True
    )
    log.debug('mc alias set: %s', result.stdout.strip() or result.stderr.strip())
    return alias


def _cleanup_remote_directory_markers(prefix_path: str) -> int:
    """删除远端零字节目录标记对象（key 以 / 结尾）。

    使用 mc ls --recursive 列出所有对象，找到 size=0 且 path 以 / 结尾的对象，
    用 mc rm 删除它们。返回删除数量。
    """
    deleted = 0
    try:
        result = subprocess.run(
            ['mc', 'ls', '--recursive', prefix_path],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            log.debug('mc ls for dir-marker cleanup failed: %s', result.stderr[:200])
            return 0
        for line in result.stdout.splitlines():
            # mc ls output: "2024-01-01 00:00:00     0B  path/to/dir/"
            parts = line.split()
            if len(parts) < 4:
                continue
            size_str = parts[2]
            path_part = parts[3]
            if not path_part.endswith('/'):
                continue
            # size field: "0B" means zero bytes
            if size_str not in ('0B', '0'):
                continue
            full_key = f'{prefix_path}/{path_part}'.replace('//', '/')
            try:
                rm_result = subprocess.run(
                    ['mc', 'rm', '--force', full_key],
                    capture_output=True, text=True, timeout=30,
                )
                if rm_result.returncode == 0:
                    deleted += 1
                    log.debug('Removed directory marker: %s', full_key)
                else:
                    log.debug('mc rm failed for %s: %s', full_key, rm_result.stderr[:100])
            except Exception as e:
                log.debug('mc rm exception for %s: %s', full_key, e)
    except Exception as e:
        log.debug('_cleanup_remote_directory_markers failed: %s', e)
    if deleted:
        log.info('Cleaned up %d remote directory marker(s) under %s', deleted, prefix_path)
    return deleted


def _mc_mirror(source: str, target: str, overwrite: bool = True,
               exclude: list[str] | None = None) -> dict:
    """使用 mc mirror 同步目录，返回执行结果。"""
    cmd = ['mc', 'mirror']
    if overwrite:
        cmd.append('--overwrite')
    if exclude:
        for pat in exclude:
            cmd.extend(['--exclude', pat])
    cmd.extend([source, target])
    log.debug('mc mirror cmd: %s', ' '.join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    res = {
        'ok': result.returncode == 0,
        'stdout': result.stdout,
        'stderr': result.stderr,
        'returncode': result.returncode,
    }
    if res['ok']:
        log.debug('mc mirror ok: %s', res['stdout'].strip()[:200])
    else:
        log.warning('mc mirror failed (rc=%d): %s', res['returncode'],
                    (res['stderr'] or res['stdout']).strip()[:400])
    return res


def _cleanup_sqlite_wal(db_path: Path) -> None:
    """恢复 SQLite 前清理 WAL/SHM/JOURNAL 文件。"""
    for ext in ('-wal', '-shm', '-journal'):
        wal = db_path.parent / (db_path.name + ext)
        if wal.exists():
            try:
                wal.unlink()
                log.info('Cleaned up %s', wal)
            except OSError as e:
                log.warning('Failed to clean up %s: %s', wal, e)


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


def purge_minio_prefix(confirm: bool = False) -> dict:
    """彻底清除 MINIO_PREFIX 下的所有对象（包括零字节目录标记）。

    使用 minio SDK 的 remove_objects 批量删除接口，每批 1000 个。
    必须传入 confirm=True 才会真正执行删除。
    """
    if not MINIO_BUCKET:
        return {"ok": False, "error": "MINIO_BUCKET not configured"}

    if not MINIO_PREFIX:
        return {
            "ok": False,
            "error": "HERMES_MINIO_PREFIX is empty; refusing to purge entire bucket. Set HERMES_MINIO_PREFIX to target a specific prefix.",
        }

    client = get_client()
    prefix = object_key("")

    # 列出所有对象
    try:
        objects = list(client.list_objects(MINIO_BUCKET, prefix=prefix, recursive=True))
    except Exception as e:
        return {"ok": False, "error": f"list_objects failed: {e}"}

    total = len(objects)
    log.info("purge: found %d objects under %s/%s", total, MINIO_BUCKET, prefix or "(root)")

    if not confirm:
        log.info("purge: dry-run mode (pass --confirm to actually delete)")
        return {"ok": True, "dry_run": True, "would_delete": total}

    from minio.deleteobjects import DeleteObject  # type: ignore

    deleted = 0
    errors: list[str] = []
    batch_size = 1000

    for i in range(0, total, batch_size):
        batch = objects[i:i + batch_size]
        delete_list = [DeleteObject(obj.object_name) for obj in batch]
        try:
            errs = list(client.remove_objects(MINIO_BUCKET, delete_list))
            deleted += len(batch) - len(errs)
            for err in errs:
                errors.append(str(err))
        except Exception as e:
            errors.append(f"batch {i}-{i+len(batch)}: {e}")

    log.info("purge complete: deleted=%d errors=%d", deleted, len(errors))
    return {"ok": len(errors) == 0, "deleted": deleted, "errors": errors}


# ── Upload: state (lightweight, daemon + manual) ───────────────────────────


def _persist_state_sync_result(result: dict) -> dict:
    """Write the latest state-sync result to a durable JSON file.

    Both the daemon process and the WebUI bridge (a separate process) can read
    this file so the UI always reflects the most recent auto-sync, not just
    manually triggered ones.  Written atomically via a temp file so a reader
    never sees a partial write.

    Returns the normalized payload so callers can reuse the exact same
    ``finished_at`` / ``ok`` fields in subprocess output and UI state.
    """
    payload = {
        **result,
        "ok": bool(result.get("ok", True)),
        "finished_at": float(result.get("finished_at") or time.time()),
    }
    try:
        STATE_SYNC_RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(STATE_SYNC_RESULT_FILE.parent),
            prefix=".minio_state_sync_last_",
            suffix=".json",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, STATE_SYNC_RESULT_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:
        log.debug("Failed to persist state sync result: %s", exc)
    return payload


def _check_disk_space(path: Path, min_bytes: int = 50 * 1024 * 1024) -> bool:
    """Check if at least min_bytes are available on the filesystem containing path."""
    try:
        st = os.statvfs(path)
        avail = st.f_bavail * st.f_frsize
        return avail >= min_bytes
    except OSError:
        return False


# Persistent reference snapshot for incremental rsync (survives across syncs)
_SNAPSHOT_REF_DIR = HERMES_HOME / '.minio_snapshot_ref'


def _incremental_snapshot(src_dir: Path, snap_dir: Path, ref_dir: Path | None,
                          exclude_sensitive: bool = True) -> bool:
    """Create a consistent snapshot of src_dir using rsync with --link-dest.

    If ref_dir exists, rsync will hardlink unchanged files from ref_dir (zero
    copy cost) and only copy files that changed since last sync. This keeps
    disk overhead proportional to the delta, not the total size.

    The snapshot is a true copy (not hardlinks to src), so concurrent writes
    to src_dir do NOT affect the snapshot. Individual files may be captured
    mid-write (torn), but this is bounded to files actively being written
    during the ~millisecond rsync traversal — far better than the old approach
    where mc mirror could read torn files over a multi-second window.

    Returns True on success.
    """
    snap_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        'rsync', '-a', '--delete',
        '--timeout=30',
    ]

    if ref_dir and ref_dir.is_dir():
        cmd.extend(['--link-dest', str(ref_dir)])

    if exclude_sensitive:
        for pat in SENSITIVE_PATHS:
            cmd.extend(['--exclude', pat])
        # Exclude SQLite WAL/SHM/journal
        cmd.extend([
            '--exclude', '*.db-wal',
            '--exclude', '*.db-shm',
            '--exclude', '*.db-journal',
        ])

    cmd.extend([str(src_dir).rstrip('/') + '/', str(snap_dir).rstrip('/') + '/'])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode not in (0, 24):  # 24 = "vanished source files" (OK)
        log.warning('rsync snapshot failed (rc=%d): %s',
                    result.returncode, result.stderr.strip()[:300])
        return False
    return True


def _check_bucket_quota_before_sync() -> dict | None:
    """Return an error dict if prefix usage exceeds MINIO_MAX_BYTES, else None."""
    if MINIO_MAX_BYTES <= 0:
        return None
    try:
        used = compute_prefix_used_bytes()
    except Exception:
        return None
    if used > MINIO_MAX_BYTES:
        return {'ok': False, 'error': 'bucket quota exceeded', 'used_bytes': used, 'max_bytes': MINIO_MAX_BYTES}
    return None


def sync_state_to_minio() -> dict:
    """Upload Hermes state (allowlist) to MinIO with consistent snapshots.

    Strategy:
    - SQLite: backup API (already safe)
    - Directories: rsync to a temp snapshot dir with --link-dest referencing
      the previous snapshot. This gives a true copy (immune to concurrent
      writes) with disk cost proportional only to changed files.
    - Single files: direct copy to staging (trivial size)
    - Degraded mode: if disk is too low or rsync unavailable, falls back to
      direct mc mirror (old behavior, less consistent but still functional).

    Returns a result dict: {"uploaded": int, "errors": [str], "mode": "state"}.
    """
    try:
        with _SyncLock(_STATE_SYNC_LOCK_FILE):
            return _sync_state_to_minio_impl()
    except (OSError, IOError):
        log.info("State sync skipped: another sync is already in progress")
        return {"ok": True, "mode": "state", "uploaded": 0, "errors": [], "skipped_reason": "concurrent"}


def _sync_state_to_minio_impl() -> dict:
    """Internal implementation of state sync (called under lock)."""
    log.debug('sync_state_to_minio: SYNC_INCLUDES_HOME=%s', SYNC_INCLUDES_HOME)
    log.debug('sync_state_to_minio: SENSITIVE_PATHS=%s', SENSITIVE_PATHS)

    quota_err = _check_bucket_quota_before_sync()
    if quota_err:
        return quota_err

    mc_available = subprocess.run(['which', 'mc'], capture_output=True).returncode == 0
    if not mc_available:
        return _sync_state_to_minio_sdk()

    try:
        alias = _configure_mc_alias()
    except Exception as e:
        log.warning('mc alias set failed (%s); falling back to SDK path', e)
        return _sync_state_to_minio_sdk()

    bucket_path = f'{alias}/{MINIO_BUCKET}'
    prefix_path = f'{bucket_path}/{MINIO_PREFIX}' if MINIO_PREFIX else bucket_path
    exclude_args = list(SENSITIVE_PATHS)

    uploaded = 0
    errors: list[str] = []
    sqlite_dbs = ['state.db', 'kanban.db', 'response_store.db']

    # Check prerequisites for snapshot mode
    can_snapshot = (
        _check_disk_space(HERMES_HOME, min_bytes=50 * 1024 * 1024)
        and subprocess.run(['which', 'rsync'], capture_output=True).returncode == 0
    )
    if not can_snapshot:
        log.warning('Snapshot mode unavailable (low disk or no rsync) — using direct mirror')

    with tempfile.TemporaryDirectory(prefix='hermes_sync_') as tmpdir:
        tmp = Path(tmpdir)

        # ── Phase 1: SQLite safe backup ──
        db_tmp = tmp / 'dbs'
        db_tmp.mkdir()
        for db_name in sqlite_dbs:
            db_path = HERMES_HOME / db_name
            if db_path.exists():
                backup_path = db_tmp / db_name
                try:
                    safe_sqlite_backup(db_path, backup_path)
                except Exception as e:
                    log.warning('Failed to backup %s: %s', db_name, e)
                    errors.append(f'{db_name}: {e}')
        if any((db_tmp / db).exists() for db in sqlite_dbs):
            res = _mc_mirror(
                str(db_tmp) + '/',
                f'{prefix_path}/home/',
                overwrite=True,
                exclude=None,
            )
            if res['ok']:
                uploaded += 1
            else:
                errors.append('sqlite dbs: mc mirror failed')

        # ── Phase 2: Classify non-DB items ──
        dir_items = []
        file_items = []
        for item in SYNC_INCLUDES_HOME:
            if item in sqlite_dbs:
                continue
            full_path = HERMES_HOME / item
            if not full_path.exists():
                continue
            if full_path.is_dir():
                dir_items.append(item)
            elif full_path.is_file():
                if not is_sensitive(item):
                    file_items.append(item)

        # ── Phase 3: Snapshot directories ──
        snap_dir = tmp / 'snapshot'
        snap_dir.mkdir()

        if can_snapshot:
            # Use previous snapshot as link-dest reference for incremental copy
            ref_dir = _SNAPSHOT_REF_DIR if _SNAPSHOT_REF_DIR.is_dir() else None

            for item in dir_items:
                src = HERMES_HOME / item
                dest = snap_dir / item
                item_ref = (ref_dir / item) if ref_dir else None
                ok = _incremental_snapshot(src, dest, item_ref)
                if not ok:
                    # Fallback: direct mirror for this item
                    log.warning('Snapshot failed for %s, using direct mirror', item)
                    res = _mc_mirror(
                        str(src) + '/',
                        f'{prefix_path}/home/{item}/',
                        overwrite=True,
                        exclude=exclude_args,
                    )
                    if res['ok']:
                        uploaded += 1
                    else:
                        errors.append(f'{item}: direct mirror failed')

            # Snapshot individual files (trivial copy)
            for item in file_items:
                src = HERMES_HOME / item
                dest = snap_dir / item
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    import shutil
                    shutil.copy2(str(src), str(dest))
                except (FileNotFoundError, OSError):
                    continue

            # Mirror the entire snapshot in one mc mirror call
            if any(snap_dir.iterdir()):
                res = _mc_mirror(
                    str(snap_dir) + '/',
                    f'{prefix_path}/home/',
                    overwrite=True,
                    exclude=exclude_args,
                )
                if res['ok']:
                    uploaded += 1
                else:
                    errors.append('snapshot mirror: mc mirror failed')

            # Rotate: current snapshot becomes next sync's reference
            try:
                if _SNAPSHOT_REF_DIR.exists():
                    import shutil
                    shutil.rmtree(_SNAPSHOT_REF_DIR)
                snap_dir.rename(_SNAPSHOT_REF_DIR)
            except OSError as e:
                log.debug('Failed to rotate snapshot ref: %s', e)
        else:
            # ── Degraded mode: direct mirror (old behavior) ──
            for item in dir_items:
                full_path = HERMES_HOME / item
                res = _mc_mirror(
                    str(full_path) + '/',
                    f'{prefix_path}/home/{item}/',
                    overwrite=True,
                    exclude=exclude_args,
                )
                if res['ok']:
                    uploaded += 1
                else:
                    errors.append(f'{item}: mc mirror failed')

            for item in file_items:
                staged = snap_dir / item
                staged.parent.mkdir(parents=True, exist_ok=True)
                try:
                    import shutil
                    shutil.copy2(str(HERMES_HOME / item), str(staged))
                except (FileNotFoundError, OSError):
                    continue
                parent_rel = str(Path(item).parent)
                target = f'{prefix_path}/home/{parent_rel}/' if parent_rel != '.' else f'{prefix_path}/home/'
                res = _mc_mirror(
                    str(staged.parent) + '/',
                    target,
                    overwrite=True,
                    exclude=exclude_args,
                )
                if res['ok']:
                    uploaded += 1
                else:
                    errors.append(f'{item}: mc mirror failed')

    log.info('State sync to MinIO complete (mc mirror): %d items, %d errors.',
             uploaded, len(errors))
    try:
        _cleanup_remote_directory_markers(prefix_path)
    except Exception as e:
        log.debug('Directory marker cleanup failed: %s', e)
    result = {'ok': len(errors) == 0, 'mode': 'state', 'uploaded': uploaded, 'errors': errors}
    return _persist_state_sync_result(result)


def _sync_state_to_minio_sdk() -> dict:
    """SDK-based fallback for sync_state_to_minio (used in tests / no mc).

    Uses rsync snapshot for directories when available, otherwise direct copy.
    """
    client = get_client()

    if not client.bucket_exists(MINIO_BUCKET):
        client.make_bucket(MINIO_BUCKET)

    uploaded = 0
    errors: list[str] = []
    sqlite_dbs = ["state.db", "kanban.db", "response_store.db"]
    can_snapshot = (
        _check_disk_space(HERMES_HOME, min_bytes=50 * 1024 * 1024)
        and subprocess.run(['which', 'rsync'], capture_output=True).returncode == 0
    )

    with tempfile.TemporaryDirectory(prefix="hermes_sync_") as tmpdir:
        tmp = Path(tmpdir)

        # SQLite: safe backup
        for db_name in sqlite_dbs:
            db_path = HERMES_HOME / db_name
            if db_path.exists():
                backup_path = tmp / db_name
                try:
                    safe_sqlite_backup(db_path, backup_path)
                    client.fput_object(
                        MINIO_BUCKET, object_key(f"home/{db_name}"), str(backup_path)
                    )
                    uploaded += 1
                except Exception as e:
                    log.warning("Failed to backup/upload %s: %s", db_name, e)
                    errors.append(f"{db_name}: {e}")

        # Non-DB items
        snap_dir = tmp / "snapshot"
        snap_dir.mkdir()
        ref_dir = _SNAPSHOT_REF_DIR if _SNAPSHOT_REF_DIR.is_dir() else None

        for item in SYNC_INCLUDES_HOME:
            if item in sqlite_dbs:
                continue
            full_path = HERMES_HOME / item
            if not full_path.exists():
                continue
            if full_path.is_file():
                if is_sensitive(item):
                    continue
                dest = snap_dir / item
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    import shutil
                    shutil.copy2(str(full_path), str(dest))
                except (FileNotFoundError, OSError):
                    continue
            elif full_path.is_dir():
                if can_snapshot:
                    dest = snap_dir / item
                    item_ref = (ref_dir / item) if ref_dir else None
                    _incremental_snapshot(full_path, dest, item_ref)
                else:
                    # Direct upload without snapshot
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
                            errors.append(f"{rel}: {e}")
                    continue

        # Upload all snapshot files
        for fpath in snap_dir.rglob("*"):
            if not fpath.is_file():
                continue
            rel = str(fpath.relative_to(snap_dir))
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

        # Rotate snapshot reference
        if can_snapshot:
            try:
                if _SNAPSHOT_REF_DIR.exists():
                    import shutil
                    shutil.rmtree(_SNAPSHOT_REF_DIR)
                snap_dir.rename(_SNAPSHOT_REF_DIR)
            except OSError:
                pass

    log.info("State sync to MinIO complete: %d objects uploaded.", uploaded)
    result = {"ok": len(errors) == 0, "mode": "state", "uploaded": uploaded, "errors": errors}
    return _persist_state_sync_result(result)


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


def compute_bucket_used_bytes(client=None, max_objects: int = 500000) -> int:
    """Return total bytes stored in the ENTIRE bucket (all prefixes).

    Used for the UI '已用空间' display so users see the full bucket usage,
    not just their own prefix.
    """
    if not MINIO_BUCKET:
        return 0
    try:
        if client is None:
            client = get_client()
    except Exception:  # pragma: no cover
        return 0
    total = 0
    seen = 0
    try:
        for obj in client.list_objects(MINIO_BUCKET, prefix="", recursive=True):
            seen += 1
            if seen > max_objects:
                break
            size = getattr(obj, "size", None)
            if isinstance(size, int) and size >= 0:
                total += size
    except Exception as exc:  # pragma: no cover
        log.debug("compute_bucket_used_bytes failed: %s", exc)
        return 0
    return int(total)


def list_remote_files(client=None, max_objects: int = 1000) -> list[dict]:
    """List files in MinIO under the configured prefix.

    Returns a list of dicts with 'path', 'size', and 'last_modified'.
    """
    if not MINIO_BUCKET:
        return []
    try:
        if client is None:
            client = get_client()
    except Exception:  # pragma: no cover
        return []
    prefix = object_key("")
    results: list[dict] = []
    try:
        for obj in client.list_objects(MINIO_BUCKET, prefix=prefix, recursive=True):
            name = getattr(obj, "object_name", None)
            if not name:
                continue
            # Strip the prefix to show relative paths
            rel = name
            if MINIO_PREFIX and rel.startswith(MINIO_PREFIX + "/"):
                rel = rel[len(MINIO_PREFIX) + 1:]
            size = getattr(obj, "size", 0) or 0
            last_modified = getattr(obj, "last_modified", None)
            ts = last_modified.isoformat() if last_modified else None
            results.append({"path": rel, "size": int(size), "last_modified": ts})
            if len(results) >= max_objects:
                break
    except Exception as exc:  # pragma: no cover
        log.debug("list_remote_files failed: %s", exc)
        return []
    return results


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
    admin = None
    try:
        admin = MinioAdmin(
            MINIO_ENDPOINT,
            credentials=None,  # built from MINIO_ACCESS_KEY/SECRET_KEY env vars
            secure=MINIO_SECURE,
        )
    except Exception:  # pragma: no cover - depends on SDK version
        # Construction signatures differ across SDK versions; treat as no-op.
        return 0
    try:
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
    finally:
        # minio 7.2.20: MinioAdmin.__del__ calls self._http.clear() which
        # raises during interpreter shutdown when module refs are already
        # gone. Eagerly clear the pool here so __del__ becomes a no-op, and
        # suppress any error from the clear itself for forward-compat.
        if admin is not None:
            try:
                http = getattr(admin, "_http", None)
                if http is not None:
                    http.clear()
            except Exception:
                pass


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

    quota_err = _check_bucket_quota_before_sync()
    if quota_err:
        return quota_err

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
    """Download state from MinIO to local paths using mc mirror when available."""
    # Fall back to SDK path when mc is not available (tests / no mc binary)
    mc_available = subprocess.run(['which', 'mc'], capture_output=True).returncode == 0

    if not mc_available:
        return _restore_from_minio_sdk()

    try:
        alias = _configure_mc_alias()
    except Exception as e:
        log.warning('mc alias set failed (%s); falling back to SDK restore', e)
        return _restore_from_minio_sdk()

    bucket_path = f'{alias}/{MINIO_BUCKET}'
    prefix_path = f'{bucket_path}/{MINIO_PREFIX}' if MINIO_PREFIX else bucket_path

    # Log remote object list for debug
    try:
        ls_result = subprocess.run(
            ['mc', 'ls', '--recursive', f'{prefix_path}/'],
            capture_output=True, text=True
        )
        log.debug('Remote objects under %s:\n%s', prefix_path,
                  ls_result.stdout[:2000] or '(empty)')
    except Exception as e:
        log.debug('mc ls failed: %s', e)

    # Clean up SQLite WAL files before restore
    for db_name in ('state.db', 'kanban.db', 'response_store.db'):
        _cleanup_sqlite_wal(HERMES_HOME / db_name)

    exclude_args = list(SENSITIVE_PATHS)

    restored_any = False

    # Restore home/
    home_src = f'{prefix_path}/home/'
    log.info('Restoring home/ from %s -> %s', home_src, HERMES_HOME)
    HERMES_HOME.mkdir(parents=True, exist_ok=True)
    res = _mc_mirror(home_src, str(HERMES_HOME) + '/', overwrite=True, exclude=exclude_args)
    if res['ok']:
        restored_any = True
        log.info('home/ restore ok')
    else:
        log.warning('home/ restore failed: %s', res['stderr'][:400])

    # Restore workspace/
    ws_src = f'{prefix_path}/workspace/'
    log.info('Restoring workspace/ from %s -> %s', ws_src, HERMES_WORKSPACE)
    HERMES_WORKSPACE.mkdir(parents=True, exist_ok=True)
    res = _mc_mirror(ws_src, str(HERMES_WORKSPACE) + '/', overwrite=True)
    if res['ok']:
        restored_any = True
        log.info('workspace/ restore ok')
    else:
        log.warning('workspace/ restore failed: %s', res['stderr'][:400])

    # Fix ownership
    try:
        subprocess.run(
            ['chown', '-R', f'{TARGET_UID}:{TARGET_GID}',
             str(HERMES_HOME), str(HERMES_WORKSPACE)],
            capture_output=True
        )
    except Exception as e:
        log.debug('chown after restore failed: %s', e)

    # Print top-2-level directory tree for debug
    for base in (HERMES_HOME, HERMES_WORKSPACE):
        if base.exists():
            try:
                entries = []
                for p in sorted(base.iterdir()):
                    entries.append(f'  {p.name}/')
                    if p.is_dir():
                        for pp in sorted(p.iterdir())[:10]:
                            entries.append(f'    {pp.name}')
                log.debug('Post-restore tree %s:\n%s', base, '\n'.join(entries[:50]))
            except Exception:
                pass

    log.info('Restore from MinIO complete (mc mirror).')
    return restored_any


def _restore_from_minio_sdk():
    """SDK-based fallback for restore_from_minio (used in tests / no mc)."""
    client = get_client()

    if not client.bucket_exists(MINIO_BUCKET):
        log.info("Bucket %s does not exist; skipping restore.", MINIO_BUCKET)
        return False

    prefix = object_key("")
    objects = list(client.list_objects(MINIO_BUCKET, prefix=prefix, recursive=True))
    if not objects:
        log.info("No backup content found at %s/%s; skipping restore.", MINIO_BUCKET, prefix)
        return False

    log.debug('restore: found %d remote objects under %s/%s', len(objects), MINIO_BUCKET, prefix)

    # Clean up SQLite WAL files before restore
    for db_name in ('state.db', 'kanban.db', 'response_store.db'):
        _cleanup_sqlite_wal(HERMES_HOME / db_name)

    restored = 0
    for obj in objects:
        rel = obj.object_name
        if MINIO_PREFIX:
            rel = rel[len(MINIO_PREFIX) + 1:]

        if rel.startswith("home/"):
            local_rel = rel[5:]
            if is_sensitive(local_rel) or not is_allowed_home_path(local_rel):
                continue
            dest = HERMES_HOME / local_rel
            stop_at = HERMES_HOME
        elif rel.startswith("workspace/"):
            local_rel = rel[10:]
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
            log.debug('Restored %s -> %s', obj.object_name, dest)
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
    # Perform an immediate sync so the WebUI shows a result right after startup
    # instead of waiting a full interval.
    try:
        sync_state_to_minio()
    except Exception as e:
        log.error("Initial state sync failed: %s", e)
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

    purge = sub.add_parser("purge", help="delete ALL objects under MINIO_PREFIX (destructive)")
    purge.add_argument(
        "--confirm",
        action="store_true",
        help="actually perform deletion (without this flag, dry-run only)",
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
    if args.cmd == "purge":
        result = purge_minio_prefix(confirm=bool(args.confirm))
        _emit_result(result)
        return 0 if result.get("ok") else 1

    parser.print_usage()
    return 1


if __name__ == "__main__":
    sys.exit(main())
