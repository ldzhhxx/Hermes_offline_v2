"""Tests for scripts/minio_sync.py -- unit tests for logic not requiring live MinIO."""
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

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


def test_is_allowed_home_path():
    assert minio_sync.is_allowed_home_path("state.db")
    assert minio_sync.is_allowed_home_path("skills/my-skill/SKILL.md")
    assert minio_sync.is_allowed_home_path("webui/sessions/demo.json")
    assert not minio_sync.is_allowed_home_path("logs/agent.log")
    assert not minio_sync.is_allowed_home_path("webui/settings.json")
    assert not minio_sync.is_allowed_home_path("random/extra.txt")


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


def test_download_object_atomically_preserves_existing_file_on_failure(tmp_path):
    dest = tmp_path / "state.db"
    dest.write_text("old-state", encoding="utf-8")

    class FailingClient:
        def fget_object(self, bucket, object_name, dest_path):
            Path(dest_path).write_text("partial-state", encoding="utf-8")
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        minio_sync._download_object_atomically(FailingClient(), "bucket", "home/state.db", dest)

    assert dest.read_text(encoding="utf-8") == "old-state"
    assert not list(tmp_path.glob(".minio_restore_*"))


def test_restore_from_minio_skips_disallowed_home_paths(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()

    class FakeClient:
        def __init__(self):
            self.downloaded = []

        def bucket_exists(self, bucket):
            return True

        def list_objects(self, bucket, prefix, recursive):
            return [
                SimpleNamespace(object_name="home/state.db"),
                SimpleNamespace(object_name="home/logs/agent.log"),
                SimpleNamespace(object_name="workspace/notes/todo.txt"),
            ]

        def fget_object(self, bucket, object_name, dest_path):
            self.downloaded.append(object_name)
            Path(dest_path).write_text(object_name, encoding="utf-8")

    fake_client = FakeClient()
    monkeypatch.setattr(minio_sync, "get_client", lambda: fake_client)
    monkeypatch.setattr(minio_sync, "HERMES_HOME", home)
    monkeypatch.setattr(minio_sync, "HERMES_WORKSPACE", workspace)
    monkeypatch.setattr(minio_sync, "MINIO_PREFIX", "")
    monkeypatch.setattr(minio_sync, "MINIO_BUCKET", "test-bucket")

    assert minio_sync.restore_from_minio() is True
    assert fake_client.downloaded == ["home/state.db", "workspace/notes/todo.txt"]
    assert (home / "state.db").read_text(encoding="utf-8") == "home/state.db"
    assert not (home / "logs" / "agent.log").exists()
    assert (workspace / "notes" / "todo.txt").read_text(encoding="utf-8") == "workspace/notes/todo.txt"
