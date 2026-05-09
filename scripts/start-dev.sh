#!/usr/bin/env bash
set -Eeuo pipefail

log() {
  printf '[hermes-offline-dev] %s\n' "$*"
}

export HERMES_HOME="${HERMES_HOME:-/home/hermes/.hermes}"
export HERMES_WORKSPACE="${HERMES_WORKSPACE:-/home/hermes/workspace}"
export HERMES_AGENT_HOST="${HERMES_AGENT_HOST:-0.0.0.0}"
export HERMES_AGENT_PORT="${HERMES_AGENT_PORT:-5000}"
export HERMES_WEBUI_HOST="${HERMES_WEBUI_HOST:-0.0.0.0}"
export HERMES_WEBUI_PORT="${HERMES_WEBUI_PORT:-18789}"
export API_SERVER_ENABLED="${API_SERVER_ENABLED:-true}"
export API_SERVER_KEY="${API_SERVER_KEY:-hermes-offline-dev-default-api-key-please-change}"
export API_SERVER_HOST="${API_SERVER_HOST:-${HERMES_AGENT_HOST}}"
export API_SERVER_PORT="${API_SERVER_PORT:-${HERMES_AGENT_PORT}}"
export API_SERVER_CORS_ORIGINS="${API_SERVER_CORS_ORIGINS:-http://localhost:${HERMES_WEBUI_PORT},http://127.0.0.1:${HERMES_WEBUI_PORT}}"
export HERMES_WEBUI_AGENT_DIR="${HERMES_WEBUI_AGENT_DIR:-/opt/hermes-offline/hermes-agent}"
export HERMES_WEBUI_PYTHON="${HERMES_WEBUI_PYTHON:-/opt/hermes-offline/.venv/bin/python}"
export HERMES_WEBUI_STATE_DIR="${HERMES_WEBUI_STATE_DIR:-${HERMES_HOME}/webui}"
export HERMES_WEBUI_SKIP_ONBOARDING="${HERMES_WEBUI_SKIP_ONBOARDING:-1}"
export HERMES_WEBUI_DEFAULT_WORKSPACE="${HERMES_WEBUI_DEFAULT_WORKSPACE:-${HERMES_WORKSPACE}}"
export HERMES_DEV_WATCH_INTERVAL="${HERMES_DEV_WATCH_INTERVAL:-2}"

WATCH_DIRS=(
  "/opt/hermes-offline/hermes-agent"
  "/opt/hermes-offline/hermes-webui"
  "/opt/hermes-offline/scripts"
)

WATCH_PATTERNS=(
  "*.py"
  "*.js"
  "*.mjs"
  "*.cjs"
  "*.ts"
  "*.tsx"
  "*.jsx"
  "*.html"
  "*.css"
  "*.json"
  "*.toml"
  "*.yaml"
  "*.yml"
  "*.sh"
)

AGENT_PID=""
WEBUI_PID=""
WATCH_MARKER="/tmp/hermes-offline-dev-watch.marker"

fix_permissions() {
  mkdir -p "${HERMES_HOME}" "${HERMES_WORKSPACE}" "${HERMES_WEBUI_STATE_DIR}"
  if [[ "$(id -u)" == "0" ]]; then
    log "Fixing ownership for mounted data directories..."
    chown -R hermes:hermes "${HERMES_HOME}" "${HERMES_WORKSPACE}"
  fi
}

run_as_hermes() {
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

start_services() {
  log "Starting Hermes Agent API server on ${API_SERVER_HOST}:${API_SERVER_PORT}..."
  run_as_hermes /opt/hermes-offline /opt/hermes-offline/.venv/bin/python -m gateway.run &
  AGENT_PID=$!
  log "Hermes Agent PID: ${AGENT_PID}"

  log "Starting Hermes WebUI on ${HERMES_WEBUI_HOST}:${HERMES_WEBUI_PORT}..."
  run_as_hermes /opt/hermes-offline/hermes-webui /opt/hermes-offline/.venv/bin/python server.py &
  WEBUI_PID=$!
  log "Hermes WebUI PID: ${WEBUI_PID}"

  touch "${WATCH_MARKER}"
  log "Dev mode ready. WebUI: http://localhost:${HERMES_WEBUI_PORT} ; Agent API: http://localhost:${API_SERVER_PORT}/health"
}

stop_services() {
  local code=${1:-0}
  log "Stopping services..."
  if [[ -n "${AGENT_PID:-}" ]] && kill -0 "${AGENT_PID}" 2>/dev/null; then
    kill "${AGENT_PID}" 2>/dev/null || true
  fi
  if [[ -n "${WEBUI_PID:-}" ]] && kill -0 "${WEBUI_PID}" 2>/dev/null; then
    kill "${WEBUI_PID}" 2>/dev/null || true
  fi

  # runuser/su can leave the actual python child alive after the wrapper exits.
  # In the dev container these are the only two long-running hermes-user
  # services, so clean them explicitly before restarting to avoid stale locks
  # and "address already in use" errors.
  if command -v pkill >/dev/null 2>&1; then
    pkill -TERM -u hermes -f '/opt/hermes-offline/.venv/bin/python -m gateway.run' 2>/dev/null || true
    pkill -TERM -u hermes -f '/opt/hermes-offline/.venv/bin/python server.py' 2>/dev/null || true
  fi

  wait "${AGENT_PID:-}" 2>/dev/null || true
  wait "${WEBUI_PID:-}" 2>/dev/null || true
  sleep 2

  if command -v pkill >/dev/null 2>&1; then
    pkill -KILL -u hermes -f '/opt/hermes-offline/.venv/bin/python -m gateway.run' 2>/dev/null || true
    pkill -KILL -u hermes -f '/opt/hermes-offline/.venv/bin/python server.py' 2>/dev/null || true
  fi

  AGENT_PID=""
  WEBUI_PID=""
  return "${code}"
}

shutdown() {
  local code=${1:-0}
  stop_services "${code}" || true
  exit "${code}"
}

trap 'shutdown 143' TERM INT

has_code_changes() {
  local find_args=()
  local pattern
  for pattern in "${WATCH_PATTERNS[@]}"; do
    if [[ ${#find_args[@]} -gt 0 ]]; then
      find_args+=( -o )
    fi
    find_args+=( -name "${pattern}" )
  done

  find "${WATCH_DIRS[@]}" \
    \( -path '*/.git' -o -path '*/.git/*' -o -path '*/node_modules' -o -path '*/node_modules/*' -o -path '*/__pycache__' -o -path '*/__pycache__/*' -o -path '*/.pytest_cache' -o -path '*/.pytest_cache/*' \) -prune \
    -o -type f \( "${find_args[@]}" \) -newer "${WATCH_MARKER}" -print -quit 2>/dev/null | grep -q .
}

fix_permissions
cd /opt/hermes-offline

log "Hermes home: ${HERMES_HOME}"
log "Workspace: ${HERMES_WORKSPACE}"
log "Watching source directories for changes every ${HERMES_DEV_WATCH_INTERVAL}s: ${WATCH_DIRS[*]}"
start_services

set +e
while true; do
  if [[ -n "${AGENT_PID}" ]] && ! kill -0 "${AGENT_PID}" 2>/dev/null; then
    wait "${AGENT_PID}"
    code=$?
    log "Hermes Agent exited unexpectedly with code ${code}."
    shutdown "${code}"
  fi
  if [[ -n "${WEBUI_PID}" ]] && ! kill -0 "${WEBUI_PID}" 2>/dev/null; then
    wait "${WEBUI_PID}"
    code=$?
    log "Hermes WebUI exited unexpectedly with code ${code}."
    shutdown "${code}"
  fi

  if has_code_changes; then
    log "Source change detected. Restarting Agent and WebUI..."
    stop_services 0 || true
    start_services
  fi

  sleep "${HERMES_DEV_WATCH_INTERVAL}"
done
