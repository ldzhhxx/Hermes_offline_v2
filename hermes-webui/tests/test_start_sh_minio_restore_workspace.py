import pathlib
import re


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
START_SH = (REPO_ROOT / "scripts" / "start.sh").read_text(encoding="utf-8")


def test_minio_restore_success_resets_last_workspace_to_home_workspace():
    assert 'if start_as_hermes /opt/hermes-offline /opt/hermes-offline/.venv/bin/python /opt/hermes-offline/scripts/minio_sync.py restore; then' in START_SH, (
        'start.sh must branch on MinIO restore success so startup-only workspace reset happens only after a real restore'
    )
    assert 'printf \'%s\\n\' "${HERMES_WORKSPACE}" > "${HERMES_WEBUI_STATE_DIR}/last_workspace.txt"' in START_SH, (
        'successful MinIO restore must reset last_workspace.txt to HERMES_WORKSPACE so startup lands in /home/hermes/workspace'
    )


def test_minio_restore_failure_keeps_existing_workspace_selection():
    block_match = re.search(
        r'if \[\[ "\$\{MINIO_ENABLED\}" == "true" \]\]; then(?P<body>.*?)^fi$',
        START_SH,
        re.MULTILINE | re.DOTALL,
    )
    assert block_match, 'could not find MinIO startup block in start.sh'
    body = block_match.group('body')
    success_pos = body.find('printf \'%s\\n\' "${HERMES_WORKSPACE}" > "${HERMES_WEBUI_STATE_DIR}/last_workspace.txt"')
    warning_pos = body.find('log "WARNING: MinIO restore failed or no backup found. Starting with current local state."')
    assert success_pos != -1, 'success path must write last_workspace.txt'
    assert warning_pos != -1, 'failure path warning missing'
    assert success_pos < warning_pos, (
        'workspace reset must live only in the restore-success path, not after the failure warning'
    )
