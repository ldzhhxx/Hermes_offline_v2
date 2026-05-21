# MinIO Sync Production UX + Quota Addendum

> For Kiro: continue implementation in `/tmp/hermes_minio_branch` on branch `feature/minio-sync-modes`.
> Approved model: `claude-opus-4.7`.
> There is already an in-progress implementation adding state/workspace split and a MinIO sync panel. Build on that work; do not throw it away.

## What is already done
The branch already has these broad changes in progress:
- periodic MinIO daemon only syncs lightweight Hermes state
- workspace sync is manual from WebUI
- WebUI has a MinIO sync panel with state/workspace actions and safety options
- bridge/API code exists in `hermes-webui/api/minio_sync.py`

Preserve that work and extend it with the requirements below.

## New product requirements

### 1) Workspace sync must support selecting which files/folders to sync
The current manual workspace sync is still too coarse. Users should be able to choose what to sync.

Minimum acceptable UX:
- Show a selectable list/tree of workspace items or at least top-level directories/files under the workspace root.
- Allow users to tick/untick which directories/files are included in the manual workspace sync.
- The sync request should send those selected relative paths to the backend.
- Backend should sync only the selected subset, not the entire workspace, when selections are provided.

If a full recursive file-tree selector is too heavy for the current repo style, Kiro may implement a pragmatic first version such as:
- top-level workspace entries with checkboxes
- optional expansion for one level deeper if straightforward

But the UX must clearly let the user choose sync scope.

### 2) Show estimated selected size before sync
The UI should show:
- total size of the selected files/folders
- ideally item count too

This can be approximate but should be based on real filesystem scanning, not a hardcoded placeholder.

### 3) Show current storage usage and remaining quota
Each user typically gets a quota of about 10 GB, maybe a bit more. The UI should show:
- configured quota total
- currently used synced storage amount for this user's MinIO prefix
- remaining amount
- clear human-readable formatting (MB/GB)

Design expectation:
- This usage should be based on the relevant MinIO prefix for the user/container, not global bucket totals.
- If exact values are unavailable in one fast call, Kiro should choose a reasonable implementation and explain the tradeoff in code/comments. But prefer real prefix usage computation.

### 4) Make quota configurable from env
Need environment-based configuration for quota and related UX.
At minimum add support for something like:
- `HERMES_MINIO_QUOTA_BYTES`
  or equivalent env variable naming Kiro chooses

The UI status endpoint should expose the public quota value.

### 5) Handle users who do not have MinIO enabled/account configured
Some accounts/users will not have MinIO configured at all. In that case the UI should not just silently hide features. It should clearly show that MinIO sync is unavailable.

Desired behavior when MinIO is not enabled/configured:
- show disabled/unavailable status in the panel
- explain that this account/container does not currently have MinIO sync enabled
- if a registration URL is configured, show a clear CTA/link for requesting/activating storage

### 6) Registration URL should be injectable from env and baked into the image
Need an env-driven URL for users without MinIO access.
At minimum add support for something like:
- `HERMES_MINIO_REGISTER_URL`
  or equivalent env variable naming Kiro chooses

Requirements:
- exposed in the non-sensitive MinIO status payload
- rendered in the WebUI when MinIO is unavailable/not configured
- shown as a link/button like “Register / Request storage” or equivalent

## Backend/API requirements
Extend the existing MinIO status + trigger backend to support:

### Status payload additions
Add public, non-secret fields such as:
- enabled/configured state
- reason why unavailable if disabled/incomplete config
- quota bytes
- used bytes under current prefix
- remaining bytes
- register URL if configured
- available workspace entries for selection, with metadata:
  - relative path
  - type (file/dir)
  - size in bytes
  - optional child counts if easy

Kiro should avoid leaking secrets in this payload.

### Workspace sync request additions
Allow workspace sync requests to pass selected relative paths, e.g.:
- `paths: ["export", "projects/foo", "notes.txt"]`

Backend validation requirements:
- paths must remain under workspace root
- reject path traversal
- reject empty/invalid items cleanly
- cleanup/remove mode must only consider the selected scope, not nuke unrelated remote objects

## Safety requirements
- Default selection should be conservative; Kiro can choose whether nothing is preselected or all top-level items are preselected, but must justify via UX and warnings. I would lean toward no dangerous hidden scope.
- If quota is exceeded or would likely be exceeded, the UI/backend should block the sync or at least clearly warn before starting. Prefer blocking with a good message.
- Do not reintroduce background workspace sync.
- Do not expose secrets.
- Do not let cleanup/delete apply outside the selected workspace scope.

## Suggested acceptance criteria
1. WebUI shows MinIO unavailable state when MinIO is not enabled/configured.
2. If `HERMES_MINIO_REGISTER_URL` is set, WebUI shows a registration/request-storage link when unavailable.
3. WebUI shows workspace selectable items and total selected size.
4. WebUI shows quota total, used bytes, and remaining bytes.
5. Manual workspace sync can target selected relative paths only.
6. Backend validates selected paths safely.
7. Quota/public status info comes from a non-secret API payload.
8. Existing tests still pass, with new targeted tests for path selection/quota/status where practical.

## Notes for implementation
- Reuse the existing MinIO sync panel and bridge; extend instead of rebuilding.
- Keep changes surgical.
- It is acceptable to implement usage calculation by listing objects under the user prefix and summing sizes if there is no better source, as long as it is scoped and reasonably handled.
- It is acceptable to implement workspace selection from top-level entries first rather than a full recursive explorer, if done cleanly.

## Verification expectations
Run targeted verification after implementation, at minimum:
- `python3 -m py_compile scripts/minio_sync.py hermes-webui/api/routes.py hermes-webui/api/minio_sync.py`
- `bash -n scripts/start.sh`
- `node --check hermes-webui/static/panels.js`
- `pytest -q tests/test_minio_sync.py hermes-webui/tests/test_minio_sync_bridge.py`
- add any extra focused tests for workspace selection/quota math/status payloads
