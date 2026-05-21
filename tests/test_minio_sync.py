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


# ── New behavior: split state vs workspace sync ─────────────────────────────


class _RecordingClient:
    """Captures fput_object / remove_object calls and fakes stat_object."""

    def __init__(self, remote_objects=None, missing_codes=("NoSuchKey",)):
        self.uploaded: list[tuple[str, str]] = []
        self.removed: list[str] = []
        self.bucket_made = False
        # remote_objects: dict[object_name -> (size, etag)]
        self._remote = dict(remote_objects or {})
        self._missing_codes = missing_codes

    def bucket_exists(self, bucket):
        return True

    def make_bucket(self, bucket):
        self.bucket_made = True

    def fput_object(self, bucket, object_name, file_path):
        # Simulate a successful upload by recording size from disk so subsequent
        # safe-mode checks can observe the new object.
        size = Path(file_path).stat().st_size
        etag = "0" * 32  # placeholder; not compared in this fake
        self._remote[object_name] = (size, etag)
        self.uploaded.append((object_name, file_path))

    def remove_object(self, bucket, object_name):
        self._remote.pop(object_name, None)
        self.removed.append(object_name)

    def stat_object(self, bucket, object_name):
        if object_name not in self._remote:
            err = type("_S3Err", (Exception,), {"code": "NoSuchKey"})("NoSuchKey")
            raise err
        size, etag = self._remote[object_name]
        return SimpleNamespace(size=size, etag=etag)

    def list_objects(self, bucket, prefix, recursive=True):
        return [
            SimpleNamespace(object_name=name, size=size, etag=etag)
            for name, (size, etag) in self._remote.items()
            if name.startswith(prefix)
        ]


def _setup_workspace(tmp_path, monkeypatch, files):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for rel, content in files.items():
        path = workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(minio_sync, "HERMES_WORKSPACE", workspace)
    monkeypatch.setattr(minio_sync, "MINIO_PREFIX", "")
    monkeypatch.setattr(minio_sync, "MINIO_BUCKET", "test-bucket")
    return workspace


def test_sync_state_does_not_touch_workspace(tmp_path, monkeypatch):
    """State-only sync must never upload workspace contents."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "SOUL.md").write_text("hello soul", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "big.txt").write_text("workspace contents", encoding="utf-8")

    monkeypatch.setattr(minio_sync, "HERMES_HOME", home)
    monkeypatch.setattr(minio_sync, "HERMES_WORKSPACE", workspace)
    monkeypatch.setattr(minio_sync, "MINIO_PREFIX", "")
    monkeypatch.setattr(minio_sync, "MINIO_BUCKET", "test-bucket")
    fake = _RecordingClient()
    monkeypatch.setattr(minio_sync, "get_client", lambda: fake)

    result = minio_sync.sync_state_to_minio()
    assert result["mode"] == "state"
    uploaded_keys = [k for k, _ in fake.uploaded]
    assert "home/SOUL.md" in uploaded_keys
    assert all(not k.startswith("workspace/") for k in uploaded_keys), uploaded_keys


def test_legacy_sync_alias_is_state_only(tmp_path, monkeypatch):
    """`sync_to_minio` (legacy) must now delegate to state-only sync."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "SOUL.md").write_text("legacy", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "ignored.txt").write_text("not synced", encoding="utf-8")

    monkeypatch.setattr(minio_sync, "HERMES_HOME", home)
    monkeypatch.setattr(minio_sync, "HERMES_WORKSPACE", workspace)
    monkeypatch.setattr(minio_sync, "MINIO_PREFIX", "")
    monkeypatch.setattr(minio_sync, "MINIO_BUCKET", "test-bucket")
    fake = _RecordingClient()
    monkeypatch.setattr(minio_sync, "get_client", lambda: fake)

    minio_sync.sync_to_minio()
    uploaded_keys = [k for k, _ in fake.uploaded]
    assert all(not k.startswith("workspace/") for k in uploaded_keys)


def test_workspace_safe_mode_skips_unchanged(tmp_path, monkeypatch):
    """Safe mode skips files whose remote copy already matches by size + md5."""
    workspace = _setup_workspace(tmp_path, monkeypatch, {
        "a.txt": "alpha",
        "sub/b.txt": "bravo",
    })
    # Pre-populate the fake remote with an exact match for a.txt.
    a_size = (workspace / "a.txt").stat().st_size
    a_md5 = minio_sync._md5_of_file(workspace / "a.txt")
    fake = _RecordingClient(remote_objects={
        "workspace/a.txt": (a_size, a_md5),
    })
    monkeypatch.setattr(minio_sync, "get_client", lambda: fake)

    result = minio_sync.sync_workspace_to_minio(mode="safe")
    uploaded = [k for k, _ in fake.uploaded]
    assert uploaded == ["workspace/sub/b.txt"], uploaded
    assert result["uploaded"] == 1
    assert result["skipped"] == 1
    assert result["deleted"] == 0


def test_workspace_safe_mode_uploads_when_size_differs(tmp_path, monkeypatch):
    workspace = _setup_workspace(tmp_path, monkeypatch, {"a.txt": "alpha"})
    fake = _RecordingClient(remote_objects={
        "workspace/a.txt": (1, "deadbeef" * 4),  # wrong size
    })
    monkeypatch.setattr(minio_sync, "get_client", lambda: fake)

    result = minio_sync.sync_workspace_to_minio(mode="safe")
    assert result["uploaded"] == 1
    assert result["skipped"] == 0


def test_workspace_mirror_mode_always_overwrites(tmp_path, monkeypatch):
    """Mirror mode uploads regardless of remote state (no skip)."""
    workspace = _setup_workspace(tmp_path, monkeypatch, {
        "a.txt": "alpha",
        "sub/b.txt": "bravo",
    })
    a_size = (workspace / "a.txt").stat().st_size
    a_md5 = minio_sync._md5_of_file(workspace / "a.txt")
    fake = _RecordingClient(remote_objects={
        "workspace/a.txt": (a_size, a_md5),  # would be skipped in safe mode
    })
    monkeypatch.setattr(minio_sync, "get_client", lambda: fake)

    result = minio_sync.sync_workspace_to_minio(mode="mirror")
    uploaded = sorted(k for k, _ in fake.uploaded)
    assert uploaded == ["workspace/a.txt", "workspace/sub/b.txt"]
    assert result["skipped"] == 0
    assert result["deleted"] == 0


def test_workspace_mirror_cleanup_removes_remote_extras(tmp_path, monkeypatch):
    workspace = _setup_workspace(tmp_path, monkeypatch, {"a.txt": "alpha"})
    fake = _RecordingClient(remote_objects={
        "workspace/a.txt": (5, "oldetag"),
        "workspace/orphan.txt": (10, "stale"),
        "home/state.db": (100, "x"),  # MUST NOT be deleted by workspace cleanup
    })
    monkeypatch.setattr(minio_sync, "get_client", lambda: fake)

    result = minio_sync.sync_workspace_to_minio(mode="mirror", cleanup_remote=True)
    assert "workspace/orphan.txt" in fake.removed
    assert "home/state.db" not in fake.removed
    assert result["deleted"] == 1


def test_workspace_safe_mode_never_deletes(tmp_path, monkeypatch):
    """Safe mode must reject cleanup_remote, even if requested."""
    _setup_workspace(tmp_path, monkeypatch, {"a.txt": "alpha"})
    fake = _RecordingClient()
    monkeypatch.setattr(minio_sync, "get_client", lambda: fake)

    with pytest.raises(ValueError):
        minio_sync.sync_workspace_to_minio(mode="safe", cleanup_remote=True)


def test_invalid_workspace_mode_rejected(tmp_path, monkeypatch):
    _setup_workspace(tmp_path, monkeypatch, {"a.txt": "alpha"})
    fake = _RecordingClient()
    monkeypatch.setattr(minio_sync, "get_client", lambda: fake)

    with pytest.raises(ValueError):
        minio_sync.sync_workspace_to_minio(mode="bogus")


def test_cli_parser_known_subcommands():
    parser = minio_sync._build_parser()
    for cmd in ("restore", "sync-state", "sync", "daemon", "sync-workspace"):
        ns = parser.parse_args([cmd])
        assert ns.cmd == cmd
    ns = parser.parse_args(["sync-workspace", "--mode", "mirror", "--cleanup-remote"])
    assert ns.mode == "mirror"
    assert ns.cleanup_remote is True
    ns = parser.parse_args(["sync-workspace"])
    assert ns.mode == "safe"
    assert ns.cleanup_remote is False


def test_cli_main_sync_workspace_invokes_function(tmp_path, monkeypatch):
    """CLI 'sync-workspace' wires through to sync_workspace_to_minio."""
    captured: dict = {}

    def _fake_workspace(mode, cleanup_remote, paths=None):
        captured["mode"] = mode
        captured["cleanup_remote"] = cleanup_remote
        captured["paths"] = paths
        return {"mode": "workspace", "uploaded": 0, "skipped": 0, "deleted": 0, "errors": []}

    monkeypatch.setattr(minio_sync, "sync_workspace_to_minio", _fake_workspace)
    rc = minio_sync.main(["sync-workspace", "--mode", "mirror", "--cleanup-remote"])
    assert rc == 0
    assert captured == {"mode": "mirror", "cleanup_remote": True, "paths": None}


def test_cli_main_sync_workspace_passes_selected_paths(tmp_path, monkeypatch):
    captured: dict = {}

    def _fake_workspace(mode, cleanup_remote, paths=None):
        captured["mode"] = mode
        captured["cleanup_remote"] = cleanup_remote
        captured["paths"] = list(paths or [])
        return {"mode": "workspace", "uploaded": 0, "skipped": 0, "deleted": 0, "errors": []}

    monkeypatch.setattr(minio_sync, "sync_workspace_to_minio", _fake_workspace)
    rc = minio_sync.main([
        "sync-workspace", "--mode", "safe",
        "--paths", "export", "projects/foo", "notes.txt",
    ])
    assert rc == 0
    assert captured["mode"] == "safe"
    assert captured["paths"] == ["export", "projects/foo", "notes.txt"]


# ── New behavior: workspace path selection + safety ────────────────────────


def test_validate_workspace_paths_strips_dotslash_and_dedupes(tmp_path, monkeypatch):
    workspace = _setup_workspace(tmp_path, monkeypatch, {
        "a.txt": "alpha",
        "sub/b.txt": "bravo",
    })
    cleaned = minio_sync.validate_workspace_paths(["./a.txt", "a.txt", "sub"])
    assert cleaned == ["a.txt", "sub"]


def test_validate_workspace_paths_rejects_traversal(tmp_path, monkeypatch):
    _setup_workspace(tmp_path, monkeypatch, {"a.txt": "alpha"})
    with pytest.raises(ValueError):
        minio_sync.validate_workspace_paths(["../etc/passwd"])
    with pytest.raises(ValueError):
        minio_sync.validate_workspace_paths(["/etc/passwd"])
    with pytest.raises(ValueError):
        minio_sync.validate_workspace_paths([""])
    with pytest.raises(ValueError):
        minio_sync.validate_workspace_paths(["sub/../../escape"])


def test_validate_workspace_paths_requires_existence(tmp_path, monkeypatch):
    _setup_workspace(tmp_path, monkeypatch, {"a.txt": "alpha"})
    with pytest.raises(ValueError):
        minio_sync.validate_workspace_paths(["does_not_exist.txt"])


def test_validate_workspace_paths_rejects_symlink_outside(tmp_path, monkeypatch):
    workspace = _setup_workspace(tmp_path, monkeypatch, {"a.txt": "alpha"})
    outside = tmp_path / "outside.txt"
    outside.write_text("escape", encoding="utf-8")
    link = workspace / "evil_link"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this filesystem")
    with pytest.raises(ValueError):
        minio_sync.validate_workspace_paths(["evil_link"])


def test_workspace_sync_with_paths_uploads_only_subset(tmp_path, monkeypatch):
    workspace = _setup_workspace(tmp_path, monkeypatch, {
        "keep/file1.txt": "k1",
        "keep/file2.txt": "k2",
        "drop/file3.txt": "d3",
        "loose.txt": "loose",
    })
    fake = _RecordingClient()
    monkeypatch.setattr(minio_sync, "get_client", lambda: fake)

    result = minio_sync.sync_workspace_to_minio(mode="safe", paths=["keep", "loose.txt"])
    uploaded = sorted(k for k, _ in fake.uploaded)
    assert uploaded == [
        "workspace/keep/file1.txt",
        "workspace/keep/file2.txt",
        "workspace/loose.txt",
    ]
    assert result["selected_paths"] == ["keep", "loose.txt"]


def test_workspace_sync_with_paths_rejects_traversal(tmp_path, monkeypatch):
    _setup_workspace(tmp_path, monkeypatch, {"a.txt": "alpha"})
    fake = _RecordingClient()
    monkeypatch.setattr(minio_sync, "get_client", lambda: fake)
    with pytest.raises(ValueError):
        minio_sync.sync_workspace_to_minio(mode="safe", paths=["../../escape"])


def test_workspace_cleanup_respects_selected_scope(tmp_path, monkeypatch):
    """Mirror+cleanup with `paths=` must not delete remote files outside scope."""
    workspace = _setup_workspace(tmp_path, monkeypatch, {
        "scope/a.txt": "alpha",
    })
    fake = _RecordingClient(remote_objects={
        "workspace/scope/a.txt": (5, "old"),
        "workspace/scope/orphan.txt": (10, "stale"),
        # Outside the selected scope — must NOT be touched.
        "workspace/other/keepme.txt": (10, "x"),
        "workspace/elsewhere.txt": (1, "x"),
    })
    monkeypatch.setattr(minio_sync, "get_client", lambda: fake)

    result = minio_sync.sync_workspace_to_minio(
        mode="mirror", cleanup_remote=True, paths=["scope"],
    )
    assert "workspace/scope/orphan.txt" in fake.removed
    assert "workspace/other/keepme.txt" not in fake.removed
    assert "workspace/elsewhere.txt" not in fake.removed
    assert result["deleted"] == 1


def test_list_workspace_entries_skips_hidden(tmp_path, monkeypatch):
    workspace = _setup_workspace(tmp_path, monkeypatch, {
        "visible.txt": "v",
        "sub/inner.txt": "i",
        ".hidden_file": "h",
    })
    (workspace / ".hidden_dir").mkdir()
    (workspace / ".hidden_dir" / "x.txt").write_text("x", encoding="utf-8")
    entries = minio_sync.list_workspace_entries()
    paths = [e["path"] for e in entries]
    assert "visible.txt" in paths
    assert "sub" in paths
    assert ".hidden_file" not in paths
    assert ".hidden_dir" not in paths
    sub = next(e for e in entries if e["path"] == "sub")
    assert sub["type"] == "dir"
    assert sub["child_count"] >= 1
    assert sub["size"] > 0
    visible = next(e for e in entries if e["path"] == "visible.txt")
    assert visible["type"] == "file"
    assert visible["size"] == len("v")


def test_compute_prefix_used_bytes_sums_listed_objects(tmp_path, monkeypatch):
    monkeypatch.setattr(minio_sync, "MINIO_BUCKET", "test-bucket")
    monkeypatch.setattr(minio_sync, "MINIO_PREFIX", "user-x/diagent")
    fake = _RecordingClient(remote_objects={
        "user-x/diagent/home/state.db": (100, "x"),
        "user-x/diagent/workspace/a.txt": (250, "x"),
        "user-x/diagent/workspace/sub/b.txt": (500, "x"),
    })
    used = minio_sync.compute_prefix_used_bytes(client=fake)
    assert used == 850


def test_cli_main_sync_state_alias(monkeypatch):
    """Legacy 'sync' CLI keyword must still call state sync."""
    called = []

    def _fake_state():
        called.append(True)
        return {"mode": "state", "uploaded": 0, "errors": []}

    monkeypatch.setattr(minio_sync, "sync_state_to_minio", _fake_state)
    assert minio_sync.main(["sync"]) == 0
    assert called == [True]

