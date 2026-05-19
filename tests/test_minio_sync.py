"""Tests for scripts/minio_sync.py -- unit tests for logic not requiring live MinIO."""
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import minio_sync


def test_is_sensitive():
    assert minio_sync.is_sensitive(".env")
    assert minio_sync.is_sensitive("auth.json")
    assert minio_sync.is_sensitive("config.yaml")
    assert minio_sync.is_sensitive("webui/settings.json")
    assert minio_sync.is_sensitive("webui/.sessions.json")
    assert minio_sync.is_sensitive("auth.lock")
    assert minio_sync.is_sensitive("state.db-wal")
    assert minio_sync.is_sensitive("state.db-shm")
    assert minio_sync.is_sensitive("kanban.db-journal")

    assert not minio_sync.is_sensitive("skills/my-skill/SKILL.md")
    assert not minio_sync.is_sensitive("state.db")
    assert not minio_sync.is_sensitive("SOUL.md")
    assert not minio_sync.is_sensitive("sessions/abc.json")
    assert not minio_sync.is_sensitive("webui/models_cache.json")
    assert not minio_sync.is_sensitive("webui/sessions/x.json")


def test_object_key_with_prefix():
    minio_sync.MINIO_PREFIX = "user-liudezheng/diagent"
    assert minio_sync.object_key("home/state.db") == "user-liudezheng/diagent/home/state.db"
    minio_sync.MINIO_PREFIX = ""
    assert minio_sync.object_key("home/state.db") == "home/state.db"


def test_safe_sqlite_backup():
    with tempfile.TemporaryDirectory() as tmpdir:
        src = Path(tmpdir) / "test.db"
        dst = Path(tmpdir) / "test_backup.db"

        # Create a test DB
        conn = sqlite3.connect(str(src))
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'hello')")
        conn.commit()
        conn.close()

        minio_sync.safe_sqlite_backup(src, dst)

        # Verify backup
        conn = sqlite3.connect(str(dst))
        rows = conn.execute("SELECT val FROM t WHERE id=1").fetchall()
        conn.close()
        assert rows == [("hello",)]


def test_safe_sqlite_backup_nonexistent():
    with tempfile.TemporaryDirectory() as tmpdir:
        src = Path(tmpdir) / "nonexistent.db"
        dst = Path(tmpdir) / "backup.db"
        # Should not raise
        minio_sync.safe_sqlite_backup(src, dst)
        assert not dst.exists()
