<div align="center">

# 🛰️ Hermes Offline v2

**面向内网与无公网环境的 Hermes 一体化离线部署方案**

![Deployment](https://img.shields.io/badge/Deployment-Docker%20一体化-0db7ed?style=flat-square)
![Mode](https://img.shields.io/badge/Runtime-离线可运行-10b981?style=flat-square)
![Skills](https://img.shields.io/badge/Skills-offline--office-8b5cf6?style=flat-square)
![API](https://img.shields.io/badge/Agent%20API-:5000-f59e0b?style=flat-square)
![WebUI](https://img.shields.io/badge/WebUI-:18789-ec4899?style=flat-square)

</div>

---

## ✨ 项目概览

Hermes Offline v2 是 Hermes 的**离线化改造版本**，把 Agent 后端与 Web UI 整合成一个可在**纯内网环境**中构建与运行的 Docker 镜像。项目针对以下场景做了系统性优化：

- 🏢 **内网 / 保密环境**：默认关闭对公网 API、云服务、社交平台、在线搜索等依赖
- 📦 **一键部署**：单镜像同时启动 Agent API + WebUI，绑定持久化数据卷
- 📝 **办公场景友好**：内置 `offline-office` 技能集，覆盖文档、表格、会议、报告等日常办公能力
- 🔁 **开发友好**：提供热重载开发模式，改代码免重新构建镜像

> ℹ️ **关于模型推理**：本仓库本身不内置大模型。使用时仍需可达的模型服务，推荐在内网部署 OpenAI 兼容接口 / Ollama / vLLM / LM Studio，然后在 Hermes 中配置内网模型端点。

---

## 📁 仓库结构

本仓库包含两个子项目目录：

| 目录            | 说明                                  | 原项目名                 |
| --------------- | ------------------------------------- | ------------------------ |
| `hermes-agent`  | Hermes Agent 后端（Python / API）     | `hermes-agent-main`      |
| `hermes-webui`  | Hermes Web UI 前端                     | `hermes-webui-master`    |

根目录提供一体化部署所需的构建与启动文件：

| 文件                     | 用途                                              |
| ------------------------ | ------------------------------------------------- |
| `Dockerfile`             | 构建包含 Python、Node.js、Agent、WebUI 的镜像      |
| `scripts/start.sh`       | 容器入口：同时启动 Agent API 与 WebUI              |
| `scripts/start-dev.sh`   | 开发模式入口：监听源码变化并自动重启               |
| `.env.docker.example`    | 可选环境变量示例                                  |

---

## 🚀 快速开始（3 步启动）

```bash
# 0) 先编辑 image-config/.env，至少设置：
# HERMES_LAUNCH_KEY_REQUIRED=my-secret-2026

# 1) 构建镜像
docker build -t hermes-offline:latest .

# 2) 启动服务（数据与工作区自动持久化到宿主机）
docker run -d \
  --name hermes-offline \
  -p 18789:18789 \
  -p 5000:5000 \
  -e HERMES_LAUNCH_KEY=my-secret-2026 \
  -v $(pwd)/data:/home/hermes/.hermes \
  -v $(pwd)/workspace:/home/hermes/workspace \
  hermes-offline:latest

# 3) 在浏览器访问
http://localhost:18789
```

> ✅ 镜像构建完成后，第 2、3 步可以在**完全离线**的环境中执行。

### 默认访问地址

| 服务              | 地址                        |
| ----------------- | --------------------------- |
| 🌐 Hermes WebUI    | `http://localhost:18789`    |
| 🔌 Hermes Agent API | `http://localhost:5000`     |

---

## 🌐 构建期 vs 运行期的离线行为

这是使用本项目时最需要明确的一件事：

| 阶段            | 是否联网                    | 执行的动作                                             |
| --------------- | --------------------------- | ------------------------------------------------------ |
| 🛠️ `docker build` | **需要源可达**（公网或内网）| 安装 Python / Node 依赖、拷贝项目代码                  |
| 🚀 容器运行时    | **无需任何联网**            | 直接启动 Agent 与 WebUI，**不会**执行 `pip install` / `npm install` / `apt install` |

> 💡 因此常见做法是：在有网络的机器上构建镜像 → 导出 `docker save` → 在内网导入 `docker load` → 直接运行。

---

## 🧰 离线内置技能集（offline-office）

本版本已**精简内置 skills**：移除了依赖公网 API、云服务、社交平台、在线搜索、GitHub、HuggingFace、在线媒体等能力的技能，新增 `offline-office` 分类，面向内网办公场景。

| 技能 ID                         | 用途                                        |
| ------------------------------- | ------------------------------------------- |
| `offline-document-drafting`     | 📝 离线文档起草、润色、摘要、改写             |
| `local-spreadsheet-csv-analysis`| 📊 本地 CSV / TSV 表格分析与清洗              |
| `meeting-minutes-offline`       | 🗒️ 会议记录整理成纪要、决议和待办             |
| `local-file-organization`       | 🗂️ 本地文件整理、目录规划、归档建议           |
| `offline-report-builder`        | 📑 基于本地资料生成报告与执行摘要             |
| `local-markdown-workflow`       | 🧾 Markdown 创建、合并、拆分、校对            |
| `local-presentation-outline`    | 🎤 汇报 / PPT 大纲与讲稿生成                  |
| `local-text-data-cleaning`      | 🧹 文本、日志、JSONL / CSV 数据清洗           |

这些技能**默认不要求联网**，也不会建议在使用阶段执行 `pip install`、`npm install` 或调用公网服务。

---

## 🐳 Docker 部署详解

### 标准运行：`docker run`

```bash
docker run -d \
  --name hermes-offline \
  -p 18789:18789 \
  -p 5000:5000 \
  -e HERMES_LAUNCH_KEY=my-secret-2026 \
  -v $(pwd)/data:/home/hermes/.hermes \
  -v $(pwd)/workspace:/home/hermes/workspace \
  hermes-offline:latest
```

若需自定义 Agent API Key：

```bash
docker run -d \
  --name hermes-offline \
  -p 18789:18789 \
  -p 5000:5000 \
  -e HERMES_LAUNCH_KEY=my-secret-2026 \
  -e API_SERVER_KEY="$(openssl rand -hex 32)" \
  -v $(pwd)/data:/home/hermes/.hermes \
  -v $(pwd)/workspace:/home/hermes/workspace \
  hermes-offline:latest
```

### 数据卷说明

默认挂载两个持久化目录：

| 宿主机路径       | 容器内路径               | 作用                                             |
| ---------------- | ------------------------ | ------------------------------------------------ |
| `./data`         | `/home/hermes/.hermes`   | Hermes 配置、会话、WebUI 状态等持久化数据       |
| `./workspace`    | `/home/hermes/workspace` | 容器内默认工作区                                  |

### 启动脚本行为（`scripts/start.sh`）

1. 创建 `/home/hermes/.hermes` 和 `/home/hermes/workspace`
2. 启动 Hermes Agent API，监听 `${HERMES_AGENT_HOST}:${HERMES_AGENT_PORT}`
3. 启动 Hermes WebUI，监听 `${HERMES_WEBUI_HOST}:${HERMES_WEBUI_PORT}`
4. 前台运行，监控两个进程；任一服务异常退出时输出明确日志并退出容器

---

## ⚙️ 环境变量

### 启动密钥门禁（Launch Key Guard）

镜像内置了一道启动门禁，防止镜像被他人直接运行。

| 变量 | 说明 |
|------|------|
| `HERMES_LAUNCH_KEY_REQUIRED` | **build 阶段**烘焙进镜像的期望密钥，在 `image-config/.env` 中设置 |
| `HERMES_LAUNCH_KEY` | **运行时**必须通过 `-e` 显式传入，与镜像内值比对 |

> ⚠️ **两者均非空且相等时，容器才会启动。** 只设置 build 时密钥还不够，运行时还必须显式传入匹配值。
>
> 此变量与 `API_SERVER_KEY`（Agent API 访问鉴权）完全独立，用途不同，请勿混淆。
>
> 构建时 `image-config/.env` 中的 `HERMES_LAUNCH_KEY_REQUIRED` 会额外烘焙到镜像内的 `/opt/hermes-offline/image-config/.env.baked`，启动脚本优先从这里读取，因此不会被 `docker run -v $(pwd)/data:/home/hermes/.hermes` 这类挂载覆盖。

**最短可用示例：**

```bash
# 1. build 前编辑 image-config/.env，设置：
# HERMES_LAUNCH_KEY_REQUIRED=my-secret-2026

# 2. 构建
docker build -t hermes-offline:latest .

# 3. 运行时传入匹配密钥
docker run -d -p 18789:18789 -p 5000:5000 \
  -e HERMES_LAUNCH_KEY=my-secret-2026 \
  hermes-offline:latest
```

---

### 服务监听地址

| 变量                   | 默认值      | 说明             |
| ---------------------- | ----------- | ---------------- |
| `HERMES_AGENT_HOST`    | `0.0.0.0`   | Agent API 绑定地址 |
| `HERMES_AGENT_PORT`    | `5000`      | Agent API 端口    |
| `HERMES_WEBUI_HOST`    | `0.0.0.0`   | WebUI 绑定地址    |
| `HERMES_WEBUI_PORT`    | `18789`     | WebUI 端口        |
| `HERMES_WEBUI_MAX_UPLOAD_MB` | `500` | WebUI 上传上限（MB），前端/后端/超时会一并按此生效 |

### Agent API 兼容变量

| 变量                     | 说明                                    |
| ------------------------ | --------------------------------------- |
| `API_SERVER_ENABLED`     | 是否启用 Agent API（`true` / `false`）  |
| `API_SERVER_HOST`        | 同 `HERMES_AGENT_HOST`                  |
| `API_SERVER_PORT`        | 同 `HERMES_AGENT_PORT`                  |
| `API_SERVER_KEY`         | Agent API 鉴权密钥                      |

> ⚠️ **安全提示**：当 Agent API 绑定 `0.0.0.0` 时**必须**设置 `API_SERVER_KEY`。镜像内提供了可运行默认值，**生产环境强烈建议通过 `-e API_SERVER_KEY=...` 或 `.env` 覆盖**。

---

## 🏗️ 使用内网源构建镜像

如果构建环境完全无外网，可以通过 build args 指定内网镜像源（下方地址仅为占位示例，请替换为实际地址）：

```bash
docker build \
  --build-arg APT_MIRROR="http://你的内网apt源/debian" \
  --build-arg PIP_INDEX_URL="http://你的内网pip源/simple" \
  --build-arg PIP_TRUSTED_HOST="你的内网pip源域名或IP" \
  --build-arg NPM_REGISTRY="http://你的内网npm源" \
  -t hermes-offline:latest .
```

若 pip 还需要额外索引：

```bash
--build-arg PIP_EXTRA_INDEX_URL="http://你的额外pip源/simple"
```

### Build Args 速查

| 参数                  | 作用                       | 不传时的默认行为                            |
| --------------------- | -------------------------- | ------------------------------------------- |
| `APT_MIRROR`          | 替换 Debian apt 源          | 使用镜像基础的默认 apt 源                    |
| `PIP_INDEX_URL`       | 替换 pip 主索引            | 使用 pip 默认配置                            |
| `PIP_EXTRA_INDEX_URL` | 增加 pip 备用索引（可选）   | 不配置                                       |
| `PIP_TRUSTED_HOST`    | 将 pip 源标记为 trusted    | 按 pip 默认规则处理                          |
| `NPM_REGISTRY`        | 替换 npm 源                 | `https://registry.npmmirror.com`             |

> 🧊 所有 build args **只作用于 `docker build` 阶段**，运行阶段容器依然不会访问网络。

---

## 🛠️ 开发模式（热重载）

适合频繁修改 `hermes-agent` 或 `hermes-webui` 源码，避免反复重建镜像。

### 工作机制

- `scripts/start-dev.sh`：每 2 秒检测源码变化，自动重启 Agent 和 WebUI
- 监听文件类型：`.py` / `.js` / `.html` / `.css` / 相关配置文件

> 📌 **前置条件**：首次进入开发模式前仍需先 `docker build` 一次基础镜像。
>
> 📌 若修改了依赖文件（`pyproject.toml`、`requirements.txt`、`package.json`、`package-lock.json`），需要重新 `docker build`。

### 启动开发模式

```bash
docker run -d \
  --name hermes-offline-dev \
  --entrypoint /opt/hermes-offline/scripts/start-dev.sh \
  -p 18789:18789 \
  -p 5000:5000 \
  -e HERMES_LAUNCH_KEY=my-secret-2026 \
  -v $(pwd)/hermes-agent:/opt/hermes-offline/hermes-agent \
  -v $(pwd)/hermes-webui:/opt/hermes-offline/hermes-webui \
  -v $(pwd)/scripts:/opt/hermes-offline/scripts \
  -v $(pwd)/data:/home/hermes/.hermes \
  -v $(pwd)/workspace:/home/hermes/workspace \
  hermes-offline:latest
```

看到类似输出说明已就绪：

```text
[hermes-offline-dev] Watching source directories for changes every 2s
[hermes-offline-dev] Dev mode ready. WebUI: http://localhost:18789
```

### 挂载映射

| 宿主机             | 容器内                                 |
| ------------------ | -------------------------------------- |
| `./hermes-agent`   | `/opt/hermes-offline/hermes-agent`     |
| `./hermes-webui`   | `/opt/hermes-offline/hermes-webui`     |
| `./scripts`        | `/opt/hermes-offline/scripts`          |

### 修改代码后的生效方式

| 修改内容                                           | 生效方式                       |
| -------------------------------------------------- | ------------------------------ |
| `hermes-agent/**/*.py`                             | 自动重启后生效                  |
| `hermes-webui/**/*.py`                             | 自动重启后生效                  |
| WebUI 静态资源（`.js` / `.html` / `.css`）          | 自动重启后刷新浏览器生效         |
| 依赖文件（`pyproject.toml` / `package.json` 等）    | **需要重新 `docker build` 镜像** |

### 停止开发模式

```bash
docker rm -f hermes-offline-dev
```

---

## 📜 日志与排查

```bash
# 生产模式日志
docker logs -f hermes-offline

# 开发模式日志
docker logs -f hermes-offline-dev
```

任一服务异常退出时，入口脚本会输出明确日志并让容器退出，便于 Docker 自动重启策略或外部编排系统检测。

---

## 🔄 迭代重建（Dockerfile.iterative）

当你已经在有网环境构建过一次完整镜像后，后续修改了前端/后端代码或 `image-config` 中的镜像内配置，可以使用 `Dockerfile.iterative` 在内网快速重建，无需重新安装所有依赖：

```bash
# 假设完整镜像已构建为 hermes-offline:base
docker build -f Dockerfile.iterative \
  --build-arg BASE_IMAGE=hermes-offline:base \
  --build-arg APT_MIRROR="http://内网apt源/debian" \
  --build-arg PIP_INDEX_URL="http://内网pip源/simple" \
  --build-arg PIP_TRUSTED_HOST="内网pip域名" \
  --build-arg NPM_REGISTRY="http://内网npm源" \
  -t hermes-offline:latest .
```

若依赖文件（`pyproject.toml`、`requirements.txt`、`package.json`）也有变更，追加 `--build-arg REINSTALL_DEPS=1`。

注意：`Dockerfile.iterative` 现在也会刷新 `image-config/config.yaml` 和 `image-config/.env`。如果运行时再额外挂载 `/home/hermes/.hermes`，仍然会覆盖镜像内配置。

---

## 🔒 模型可见性控制

离线版默认只向前端暴露 `yice` provider。如需修改可见的 provider/模型列表，需同步修改以下三处（代码中已标注 `MODEL VISIBILITY CONTROL POINT`）：

| 序号 | 文件 | 位置 |
|------|------|------|
| 1/3 | `hermes-webui/api/config.py` | `_offline_filter_models()` 函数 |
| 2/3 | `hermes-webui/api/config.py` | `_build_available_models_uncached()` 末尾 |
| 3/3 | `hermes-webui/api/providers.py` | `get_providers()` 末尾 `_OFFLINE_VISIBLE_PROVIDERS` |

---

<div align="center">

**Hermes Offline v2 · 把 Agent 能力搬进内网**

</div>
