# image-config

这个目录专门放“打进镜像里的 Hermes 配置”。

## 你平时只需要改这两个文件

### 1. `config.yaml`
放结构化配置，适合改这些：
- 默认 provider
- 默认模型
- provider 的 base_url
- 默认禁用的插件列表

当前默认是：
- provider：`custom:yice`
- model：`Qwen3.5-397B-A17B`

### 2. `.env`
放密钥和少量运行参数。
这个文件**本地自己保留，不提交 git**。

第一次用：

```bash
cp image-config/.env.example image-config/.env
```

然后至少改这个：

```env
YICE_API_KEY=***
```

如果你想自定义 API Server 的访问密钥，再额外加：

```env
API_SERVER_KEY=你自己定一个访问API用的key
```

## 最常改的地方

### 改 Yice key
改 `image-config/.env`

```env
YICE_API_KEY=你的真实key
```

### 改默认模型
改 `image-config/config.yaml`

```yaml
model:
  default: 你要开放的模型名
```

### 改 Yice 地址
如果以后网关地址变了：

1. 改 `image-config/config.yaml` 里的：
   - `model.base_url`
   - `custom_providers[0].base_url`
2. 如果你想也在 `.env` 留备注，可以顺手改 `.env.example`

### 改默认隐藏的插件
改 `image-config/config.yaml`

```yaml
plugins:
  default_disabled:
```

- 想默认隐藏：加到这个列表里
- 想恢复默认可见：从这个列表删掉

## 构建方式

确保本地已经有 `image-config/.env`，再构建：

```bash
docker build -t diagent-offline:latest .
```

## 运行方式

如果你想用“镜像内配置”模式，运行时**不要挂载 `./data` 到 `/home/hermes/.hermes`**，不然会把镜像里打进去的 `config.yaml` 和 `.env` 覆盖掉。

最简启动：

```bash
docker run -d \
  --name diagent-offline \
  -p 18789:18789 \
  -p 5000:5000 \
  diagent-offline:latest
```

## 文件职责，一句话记忆

- `config.yaml`：放配置结构
- `.env`：放 key
- `.env.example`：放模板
