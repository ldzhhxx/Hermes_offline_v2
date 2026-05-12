#!/usr/bin/env bash
set -Eeuo pipefail

log() {
  printf '[hermes-offline] %s\n' "$*"
}

shutdown() {
  local code=${1:-0}
  log "Shutting down services (exit code: ${code})..."
  if [[ -n "${AGENT_PID:-}" ]] && kill -0 "${AGENT_PID}" 2>/dev/null; then
    kill "${AGENT_PID}" 2>/dev/null || true
  fi
  if [[ -n "${WEBUI_PID:-}" ]] && kill -0 "${WEBUI_PID}" 2>/dev/null; then
    kill "${WEBUI_PID}" 2>/dev/null || true
  fi
  wait 2>/dev/null || true
  exit "${code}"
}

trap 'shutdown 143' TERM INT

BAKED_ENV_FILE="${HERMES_BAKED_ENV_FILE:-/opt/hermes-offline/image-config/.env.baked}"

# 优先读取镜像构建期烘焙进去的 env 文件。这个路径不在 /home/hermes/.hermes 下，
# 不会被 volume 覆盖，适合放启动门禁这类“镜像内固定值”。
if [[ -f "${BAKED_ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  set -a
  source "${BAKED_ENV_FILE}"
  set +a
fi

# ── 启动密钥门禁 ────────────────────────────────────────────────────────────
# HERMES_LAUNCH_KEY_REQUIRED : build 阶段烘焙进镜像的期望密钥（明文）
# HERMES_LAUNCH_KEY          : 运行时必须由调用方通过 -e 显式传入
#
# 两者均非空且相等时，才允许继续启动。
# 注意：此变量与 API_SERVER_KEY（Agent API 访问鉴权）完全独立，用途不同。
_required_key="${HERMES_LAUNCH_KEY_REQUIRED:-}"
_provided_key="${HERMES_LAUNCH_KEY:-}"

if [[ -z "${_required_key}" ]]; then
  log "ERROR: 镜像内未设置启动密钥（HERMES_LAUNCH_KEY_REQUIRED 为空）。"
  log "       请在 image-config/.env 中设置 HERMES_LAUNCH_KEY_REQUIRED=<密钥> 后重新构建镜像。"
  exit 1
fi

if [[ -z "${_provided_key}" ]]; then
  log "ERROR: 缺少启动密钥。请在 docker run 时传入 -e HERMES_LAUNCH_KEY=<密钥>。"
  exit 1
fi

if [[ "${_provided_key}" != "${_required_key}" ]]; then
  log "ERROR: 启动密钥不匹配，容器拒绝启动。"
  log "       请确认传入的 HERMES_LAUNCH_KEY 与镜像内预置的密钥一致。"
  exit 1
fi

log "启动密钥校验通过。"
unset _required_key _provided_key
# ────────────────────────────────────────────────────────────────────────────

export HERMES_HOME="${HERMES_HOME:-/home/hermes/.hermes}"
export HERMES_WORKSPACE="${HERMES_WORKSPACE:-/home/hermes/workspace}"
export HERMES_AGENT_HOST="${HERMES_AGENT_HOST:-0.0.0.0}"
export HERMES_AGENT_PORT="${HERMES_AGENT_PORT:-5000}"
export HERMES_WEBUI_HOST="${HERMES_WEBUI_HOST:-0.0.0.0}"
export HERMES_WEBUI_PORT="${HERMES_WEBUI_PORT:-18789}"

# Hermes Agent's API server still supports the historical API_SERVER_* names.
# Keep both sets in sync so old and new config paths work.
export API_SERVER_ENABLED="${API_SERVER_ENABLED:-true}"
export API_SERVER_KEY="${API_SERVER_KEY:-hermes-offline-default-api-key-please-change-2026}"
export API_SERVER_HOST="${API_SERVER_HOST:-${HERMES_AGENT_HOST}}"
export API_SERVER_PORT="${API_SERVER_PORT:-${HERMES_AGENT_PORT}}"
export API_SERVER_CORS_ORIGINS="${API_SERVER_CORS_ORIGINS:-http://localhost:${HERMES_WEBUI_PORT},http://127.0.0.1:${HERMES_WEBUI_PORT}}"
export HERMES_WEBUI_AGENT_DIR="${HERMES_WEBUI_AGENT_DIR:-/opt/hermes-offline/hermes-agent}"
export HERMES_WEBUI_PYTHON="${HERMES_WEBUI_PYTHON:-/opt/hermes-offline/.venv/bin/python}"
export HERMES_WEBUI_STATE_DIR="${HERMES_WEBUI_STATE_DIR:-${HERMES_HOME}/webui}"
export HERMES_WEBUI_SKIP_ONBOARDING="${HERMES_WEBUI_SKIP_ONBOARDING:-1}"
export HERMES_WEBUI_DEFAULT_WORKSPACE="${HERMES_WEBUI_DEFAULT_WORKSPACE:-${HERMES_WORKSPACE}}"
export HERMES_BAKED_ENV_FILE="${BAKED_ENV_FILE}"

# Docker bind mounts created by `docker run -v ./data:/home/hermes/.hermes`
# are commonly root-owned on the host. Start as root, create/chown the mounted
# directories, then run the actual services as the unprivileged hermes user.
mkdir -p "${HERMES_HOME}" "${HERMES_WORKSPACE}" "${HERMES_WEBUI_STATE_DIR}"
if [[ "$(id -u)" == "0" ]]; then
  log "Fixing ownership for mounted data directories..."
  chown -R hermes:hermes "${HERMES_HOME}" "${HERMES_WORKSPACE}"
fi

cd /opt/hermes-offline

start_as_hermes() {
  local workdir="$1"
  shift
  local cmd="cd $(printf '%q' "${workdir}") && exec"
  local arg
  for arg in "$@"; do
    cmd+=" $(printf '%q' "${arg}")"
  done

  if [[ "$(id -u)" == "0" ]]; then
    if command -v runuser >/dev/null 2>&1; then
      runuser -u hermes -- bash -lc "${cmd}"
    else
      su -m -s /bin/bash hermes -c "${cmd}"
    fi
  else
    cd "${workdir}"
    exec "$@"
  fi
}

log "Hermes home: ${HERMES_HOME}"
log "Workspace: ${HERMES_WORKSPACE}"
log "Starting Hermes Agent API server on ${API_SERVER_HOST}:${API_SERVER_PORT}..."
start_as_hermes /opt/hermes-offline /opt/hermes-offline/.venv/bin/python -m gateway.run &
AGENT_PID=$!
log "Hermes Agent PID: ${AGENT_PID}"

log "Starting Hermes WebUI on ${HERMES_WEBUI_HOST}:${HERMES_WEBUI_PORT}..."
start_as_hermes /opt/hermes-offline/hermes-webui /opt/hermes-offline/.venv/bin/python server.py &
WEBUI_PID=$!
log "Hermes WebUI PID: ${WEBUI_PID}"

log "Startup complete. WebUI: http://localhost:${HERMES_WEBUI_PORT} ; Agent API: http://localhost:${API_SERVER_PORT}/health"

set +e
while true; do
  if ! kill -0 "${AGENT_PID}" 2>/dev/null; then
    wait "${AGENT_PID}"
    code=$?
    log "Hermes Agent exited unexpectedly with code ${code}."
    shutdown "${code}"
  fi
  if ! kill -0 "${WEBUI_PID}" 2>/dev/null; then
    wait "${WEBUI_PID}"
    code=$?
    log "Hermes WebUI exited unexpectedly with code ${code}."
    shutdown "${code}"
  fi
  sleep 2
done
