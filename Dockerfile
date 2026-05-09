FROM nikolaik/python-nodejs:python3.11-nodejs20

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HERMES_HOME=/home/hermes/.hermes \
    HERMES_WORKSPACE=/home/hermes/workspace \
    HERMES_AGENT_HOST=0.0.0.0 \
    HERMES_AGENT_PORT=5000 \
    HERMES_WEBUI_HOST=0.0.0.0 \
    HERMES_WEBUI_PORT=18789 \
    API_SERVER_ENABLED=true \
    API_SERVER_HOST=0.0.0.0 \
    API_SERVER_PORT=5000 \
    API_SERVER_KEY=hermes-offline-default-api-key-please-change-2026 \
    API_SERVER_CORS_ORIGINS=http://localhost:18789,http://127.0.0.1:18789 \
    HERMES_WEBUI_AGENT_DIR=/opt/hermes-offline/hermes-agent \
    HERMES_WEBUI_PYTHON=/opt/hermes-offline/.venv/bin/python \
    HERMES_WEBUI_STATE_DIR=/home/hermes/.hermes/webui \
    HERMES_WEBUI_SKIP_ONBOARDING=1 \
    HERMES_WEBUI_DEFAULT_WORKSPACE=/home/hermes/workspace

WORKDIR /opt/hermes-offline

COPY hermes-agent ./hermes-agent
COPY hermes-webui ./hermes-webui
COPY README.md ./README.md
COPY scripts/start.sh ./scripts/start.sh

RUN python -m venv /opt/hermes-offline/.venv \
    && /opt/hermes-offline/.venv/bin/python -m pip install --upgrade pip setuptools wheel \
    && /opt/hermes-offline/.venv/bin/pip install -e './hermes-agent[messaging,cli,pty]' \
    && /opt/hermes-offline/.venv/bin/pip install -r ./hermes-webui/requirements.txt \
    && npm config set fetch-retries 5 \
    && npm config set fetch-retry-factor 2 \
    && npm config set fetch-retry-mintimeout 20000 \
    && npm config set fetch-retry-maxtimeout 120000 \
    && npm config set registry https://registry.npmmirror.com \
    && if [ -f ./hermes-agent/package-lock.json ]; then (cd ./hermes-agent && npm ci --omit=dev --ignore-scripts); else (cd ./hermes-agent && npm install --omit=dev --ignore-scripts); fi \
    && chmod +x /opt/hermes-offline/scripts/start.sh \
    && touch /.within_container

RUN useradd --create-home --home-dir /home/hermes --shell /bin/bash hermes \
    && mkdir -p /home/hermes/.hermes /home/hermes/workspace \
    && chown -R hermes:hermes /home/hermes /opt/hermes-offline

# Start as root so the entrypoint can fix ownership of bind-mounted
# ./data and ./workspace directories created by Docker on the host.
# The entrypoint then launches Hermes Agent and WebUI as the unprivileged
# hermes user.
USER root
WORKDIR /opt/hermes-offline

EXPOSE 18789 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os, urllib.request; [urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=3).read() for p in (os.getenv('HERMES_WEBUI_PORT', '18789'), os.getenv('HERMES_AGENT_PORT', '5000'))]" || exit 1

ENTRYPOINT ["/opt/hermes-offline/scripts/start.sh"]
