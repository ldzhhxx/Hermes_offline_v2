# Hermes Offline

**An offline-ready, all-in-one Docker distribution of [Hermes Agent](https://github.com/NousResearch/hermes-agent) and [Hermes WebUI](https://github.com/nesquena/hermes-webui) for air-gapped and intranet environments.**

Upstream Hermes projects assume an internet-connected machine. This repository integrates the Hermes Agent API backend and the Hermes WebUI frontend — plus Python, Node.js and all runtime dependencies — into a single Docker image that:

- installs every dependency at **build time**, so the container starts and runs with **no internet access at runtime**
- supports **intranet mirrors** at build time via build args (`APT_MIRROR`, `PIP_INDEX_URL`, `NPM_REGISTRY`, ...) for restricted corporate networks
- starts both services from one entrypoint script (Agent API on `:5000`, WebUI on `:18789`)
- ships a **dev mode** (`docker-compose.dev.yml`) that hot-restarts the agent and WebUI on source changes

This project is a packaging and integration effort: all credit for the agent and the WebUI goes to their upstream authors (see *Credits & License* below). The maintainer's own work in this repository is the offline adaptation — image build, mirror support, startup orchestration and documentation.

**Quick start**

```bash
docker compose up -d
# WebUI: http://localhost:18789 - Agent API: http://localhost:5000
```

---

