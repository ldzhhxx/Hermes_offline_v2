# Hermes_offline_v2

本仓库为 Hermes 离线版改造项目，包含两个子项目目录：

- `hermes-agent`：原 `hermes-agent-main` 改名而来
- `hermes-webui`：原 `hermes-webui-master` 改名而来

该版本以离线适配为目标，整合了 Hermes Agent 与 Hermes Web UI 的离线部署内容。

## Docker 离线部署

本仓库根目录提供一体化 Docker 镜像构建文件：

- `Dockerfile`：构建包含 Python、Node.js、Hermes Agent、Hermes WebUI 及全部运行依赖的一体化镜像
- `scripts/start.sh`：容器入口脚本，同时启动 Agent API 后端与 WebUI 前端
- `docker-compose.yml`：本地持久化数据目录与端口映射配置
- `.env.docker.example`：可选环境变量示例

镜像构建阶段会完成 Python / Node 依赖安装；容器启动阶段不会执行 `pip install` 或 `npm install`，因此镜像构建完成后可以在无公网环境中直接运行。

### 默认端口

```bash
Hermes Agent API: 0.0.0.0:5000
Hermes WebUI    : 0.0.0.0:18789
```

可通过环境变量覆盖：

```bash
HERMES_AGENT_HOST=0.0.0.0
HERMES_AGENT_PORT=5000
HERMES_WEBUI_HOST=0.0.0.0
HERMES_WEBUI_PORT=18789
```

Agent API 同时兼容原有变量：

```bash
API_SERVER_ENABLED=true
API_SERVER_HOST=0.0.0.0
API_SERVER_PORT=5000
API_SERVER_KEY=your-secure-api-key
```

> 注意：Agent API 绑定 `0.0.0.0` 时需要 `API_SERVER_KEY`。镜像内提供了可运行的默认值，生产环境建议通过 `-e API_SERVER_KEY=...` 或 `.env` 替换。

### 构建镜像

```bash
docker build -t hermes-offline:latest .
```

### 直接启动

```bash
docker run -d \
  --name hermes-offline \
  -p 18789:18789 \
  -p 5000:5000 \
  -v $(pwd)/data:/home/hermes/.hermes \
  -v $(pwd)/workspace:/home/hermes/workspace \
  hermes-offline:latest
```

如果需要设置自己的 Agent API Key：

```bash
docker run -d \
  --name hermes-offline \
  -p 18789:18789 \
  -p 5000:5000 \
  -e API_SERVER_KEY="$(openssl rand -hex 32)" \
  -v $(pwd)/data:/home/hermes/.hermes \
  -v $(pwd)/workspace:/home/hermes/workspace \
  hermes-offline:latest
```

### 使用 docker-compose 启动

```bash
docker compose up -d
```

### 查看日志

```bash
docker logs -f hermes-offline
```

### 访问地址

```bash
http://localhost:18789
```

### 数据目录

`docker-compose.yml` 默认挂载：

```bash
./data:/home/hermes/.hermes
./workspace:/home/hermes/workspace
```

其中：

- `./data` 保存 Hermes 配置、会话、WebUI 状态等持久化数据
- `./workspace` 作为容器内默认工作区

### 启动脚本行为

`scripts/start.sh` 会：

1. 创建 `/home/hermes/.hermes` 和 `/home/hermes/workspace`
2. 启动 Hermes Agent API 后端，监听 `${HERMES_AGENT_HOST}:${HERMES_AGENT_PORT}`
3. 启动 Hermes WebUI，监听 `${HERMES_WEBUI_HOST}:${HERMES_WEBUI_PORT}`
4. 保持容器前台运行
5. 监控两个进程，任一服务异常退出时输出明确日志并退出容器
