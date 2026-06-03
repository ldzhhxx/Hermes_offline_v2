import pathlib


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
START_SH = (REPO_ROOT / "scripts" / "start.sh").read_text(encoding="utf-8")


def test_start_sh_does_not_block_on_minio_restore():
    """start.sh must NOT run minio_sync.py restore synchronously.

    The restore is now handled asynchronously by the WebUI after startup
    so the user sees a loading UI with progress and can skip.
    """
    assert 'minio_sync.py restore' not in START_SH, (
        'start.sh must not run minio_sync.py restore synchronously. '
        'Restore is now handled by the WebUI API.'
    )


def test_start_sh_still_starts_minio_daemon():
    """start.sh must still start the periodic MinIO sync daemon."""
    assert 'minio_sync.py daemon' in START_SH, (
        'start.sh must still start the MinIO sync daemon for periodic state sync.'
    )


def test_start_sh_logs_minio_enabled():
    """start.sh logs when MinIO is enabled so operators see it in container logs."""
    assert 'MinIO storage mode enabled' in START_SH, (
        'start.sh must log when MinIO is enabled.'
    )
