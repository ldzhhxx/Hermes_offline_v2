#!/usr/bin/env python3
"""MinIO state sync for Hermes Offline v2.

Provides restore-on-startup and periodic-sync for k8s one-user-per-container usage.
Uses the minio Python SDK (S3-compatible).

Environment variables:
  HERMES_MINIO_ENABLED       - "true" to enable MinIO mode (default: disabled)
  HERMES_MINIO_ENDPOINT      - MinIO/S3 endpoint (e.g. minio.internal:9000)
  HERMES_MINIO_ACCESS_KEY    - Access key
  HERMES_MINIO_SECRET_KEY    - Secret key
  HERMES_MINIO_BUCKET        - Bucket name
  HERMES_MINIO_PREFIX        - Object prefix (e.g. user-liudezheng/diagent)
  HERMES_MINIO_SECURE        - "true" for HTTPS (default: "false")
  HERMES_MINIO_SYNC_INTERVAL - Sync interval in seconds (default: 300)
"""

import logging
import os
import shutil
import signal
import sqlite3
import subprocess
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

# Files/dirs to sync under HERMES_HOME (relative paths)
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


# ── Upload (Sync to MinIO) ─────────────────────────────────────────────────


def sync_to_minio():
    """Upload local state to MinIO."""
    client = get_client()

    if not client.bucket_exists(MINIO_BUCKET):
        client.make_bucket(MINIO_BUCKET)

    uploaded = 0

    # Handle SQLite databases with safe backup
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

        # Handle regular files/dirs
        for item in SYNC_INCLUDES_HOME:
            if item in sqlite_dbs:
                continue
            full_path = HERMES_HOME / item
            if not full_path.exists():
                continue
            if full_path.is_file():
                if not is_sensitive(item):
                    try:
                        client.fput_object(
                            MINIO_BUCKET, object_key(f"home/{item}"), str(full_path)
                        )
                        uploaded += 1
                    except Exception as e:
                        log.warning("Failed to upload %s: %s", item, e)
            elif full_path.is_dir():
                for fpath in full_path.rglob("*"):
                    if not fpath.is_file():
                        continue
                    rel = str(fpath.relative_to(HERMES_HOME))
                    if is_sensitive(rel):
                        continue
                    if rel.endswith(("-wal", "-shm", "-journal")):
                        continue
                    try:
                        client.fput_object(
                            MINIO_BUCKET, object_key(f"home/{rel}"), str(fpath)
                        )
                        uploaded += 1
                    except Exception as e:
                        log.warning("Failed to upload %s: %s", rel, e)

    # Sync workspace
    if HERMES_WORKSPACE.exists():
        for fpath in HERMES_WORKSPACE.rglob("*"):
            if not fpath.is_file():
                continue
            rel = str(fpath.relative_to(HERMES_WORKSPACE))
            try:
                client.fput_object(
                    MINIO_BUCKET, object_key(f"workspace/{rel}"), str(fpath)
                )
                uploaded += 1
            except Exception as e:
                log.warning("Failed to upload workspace/%s: %s", rel, e)

    log.info("Sync to MinIO complete: %d objects uploaded.", uploaded)


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
    """Run periodic sync loop."""
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("Periodic sync daemon started (interval=%ds).", SYNC_INTERVAL)
    while not _shutdown:
        time.sleep(SYNC_INTERVAL)
        if _shutdown:
            break
        try:
            sync_to_minio()
        except Exception as e:
            log.error("Sync failed: %s", e)

    # Final sync on shutdown
    log.info("Performing final sync before exit...")
    try:
        sync_to_minio()
    except Exception as e:
        log.error("Final sync failed: %s", e)
    log.info("Daemon exiting.")


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: minio_sync.py [restore|sync|daemon]")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "restore":
        restore_from_minio()
    elif cmd == "sync":
        sync_to_minio()
    elif cmd == "daemon":
        run_daemon()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
