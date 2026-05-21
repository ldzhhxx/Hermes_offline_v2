# MinIO Sync Production UX + Backend Split

> For Kiro: implement this in `/tmp/hermes_minio_branch` on branch `feature/minio-sync-modes`.
> Model chosen by user: `claude-opus-4.7`.
> Follow DRY/YAGNI. Make surgical changes. Prefer small composable helpers. Do not broaden scope beyond the requirements below.

## Product Goal

Make MinIO persistence safe for production clusters by splitting lightweight automatic state sync from explicit user-triggered workspace sync, and by adding clear WebUI controls so operators/users can understand and control what is synced.

## Current Problem

Current `scripts/minio_sync.py` behaves like repeated recursive `cp` for both state and workspace. When the workspace contains many files, first sync can overload MinIO. That is not acceptable for production clusters.

## Desired Product Behavior

### 1) Automatic sync should cover only lightweight state
Automatic periodic sync should continue for Hermes state only, not for the whole workspace.

State includes the existing allowed Hermes-home paths such as:
- `skills`
- `state.db`
- `kanban.db`
- `response_store.db`
- `SOUL.md`
- `gateway_state.json`
- `channel_directory.json`
- `platforms`
- `sessions`
- `memories`
- `cron`
- `sandboxes`
- `webui/models_cache.json`
- `webui/sessions`

Automatic sync must **not** recursively sync `workspace/` by default.

### 2) Workspace sync should be manual
Workspace sync must be a separate explicit action, triggered from WebUI by a user-facing button or controls.

### 3) Workspace sync should expose options in the UI
WebUI should provide a richer control surface for workspace sync, including at minimum:
- trigger workspace sync manually
- show current sync target summary/status
- choose whether to enable an overwrite-style mode / stronger sync mode
- ideally expose whether remote-extra cleanup is enabled or disabled in a clear way
- show an explanation/warning before dangerous options

The user explicitly wants the page to have richer controls such as whether to add an overwrite-like parameter during workspace sync.

### 4) Kiro should think through the UX and implementation details
Kiro should not just blindly add one button. It should choose a reasonable UX placement, labels, warnings, API shape, and backend command strategy consistent with the existing WebUI.

## Technical Direction

### A. Preserve current restore safety work
Do not regress the already-fixed behavior around:
- restore allowlist
- skipping logs restore
- restore as `hermes`
- ownership repair
- atomic download restore

### B. Split state sync and workspace sync in backend
The current backend should be refactored into clearly separate flows, e.g. similar to:
- `restore` -> restore state from MinIO on startup
- `sync-state` -> only sync lightweight state
- `sync-workspace` -> manual workspace sync
- `daemon` -> periodic state sync only

Names can vary if Kiro finds a better fit, but the behavior must be cleanly separated.

### C. Workspace sync should no longer be unconditional recursive Python upload by default
For workspace sync, Kiro should evaluate the safest production approach. The intended direction is closer to `mc mirror` semantics than repeated per-file `cp` semantics.

Kiro may choose one of these approaches:
1. invoke `mc mirror` directly if available and appropriate
2. implement mirror-like incremental behavior in Python if that is safer / easier to verify in this repo

But Kiro must explicitly optimize for production use, not for minimal code churn.

### D. Dangerous options must be guarded
If a workspace sync mode can overwrite remote content more aggressively, or remove remote extras, the UI/API must:
- clearly label it
- default it to the safer option
- require explicit user choice
- ideally show a warning/confirmation before running

### E. Avoid syncing the entire workspace by default in background
Automatic background sync must not crawl the whole workspace every interval.

## UX Requirements

Kiro should design a minimal but production-appropriate sync panel or settings area.

At minimum, include:
- current MinIO mode enabled/disabled status
- periodic state sync interval/status
- button: sync state now
- button: sync workspace
- one or more workspace sync options, such as:
  - safer default sync mode
  - stronger overwrite/mirror mode
  - optional cleanup/remove remote extras mode only if Kiro judges it worth exposing
- visible status/result area for the last sync attempt
- disabled/loading state while a sync is running

Kiro should reuse existing UI patterns/styles rather than inventing a separate visual system.

## API / Backend Requirements

Add server/API endpoints as needed so the WebUI can:
- fetch sync capabilities / current status
- trigger state sync immediately
- trigger workspace sync with options
- receive structured success/failure details

If there is no good async job framework in this repo already, Kiro may implement a simple pragmatic approach, but should avoid blocking the UI without feedback.

## Safety Requirements

- Do not sync secrets or auth/config files that were intentionally excluded.
- Do not reintroduce log restore.
- Do not make workspace auto-sync the default.
- Default workspace sync mode must be conservative.
- If remote deletion / cleanup is supported, it must not be the default.

## Suggested Acceptance Criteria

1. Startup restore still works and keeps the current permission fix.
2. Periodic daemon sync no longer uploads the whole workspace automatically.
3. WebUI exposes manual workspace sync controls.
4. User can manually trigger state sync from the UI.
5. User can manually trigger workspace sync with at least one safety-related option.
6. Dangerous workspace sync options are opt-in and clearly labeled.
7. Existing MinIO unit tests still pass, and new tests cover the new behavior where practical.
8. If UI/API code is added, include focused tests or at least verifiable targeted checks.

## File Hints

Likely relevant files include, but are not limited to:
- `scripts/minio_sync.py`
- `scripts/start.sh`
- `tests/test_minio_sync.py`
- `hermes-webui/static/*.js`
- `hermes-webui/static/*.html`
- `hermes-webui/api/*.py`

Kiro should inspect the repo and choose the right integration points itself.

## Verification Expectations

Kiro must not stop at code edits. It should also run targeted verification such as:
- `python3 -m py_compile scripts/minio_sync.py`
- `bash -n scripts/start.sh`
- `pytest -q tests/test_minio_sync.py`
- any targeted API/UI tests it adds

If the chosen implementation uses `mc`, Kiro must verify how the command is invoked and report assumptions if the binary is not bundled.

## Important Constraints

- Keep scope focused on MinIO production sync UX/backend split.
- Do not touch unrelated branding, provider configuration, or model logic.
- Do not broaden secrets persistence.
- Minimize surprise for operators in production clusters.
