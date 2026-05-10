# Hermes Offline v2 部署指南

## 📦 方式一：使用导出的镜像

### 1. 加载Docker镜像

```bash
# 将 hermes-offline-image.tar 文件传输到你的服务器
docker load -i hermes-offline-image.tar
```

### 2. 创建必要的目录

```bash
mkdir -p data workspace
```

### 3. 运行容器

```bash
docker run -d \
  --name hermes-offline \
  -p 18789:18789 \
  -p 5000:5000 \
  -v $(pwd)/data:/home/hermes/.hermes \
  -v $(pwd)/workspace:/home/hermes/workspace \
  -e API_SERVER_KEY="your-secure-api-key-here" \
  -e HERMES_WEBUI_PASSWORD="your-webui-password" \
  -e GATEWAY_ALLOW_ALL_USERS=true \
  hermes-offline:latest
```

### 4. 访问服务

- **WebUI**: http://your-server-ip:18789
- **Agent API**: http://your-server-ip:5000

---

## 🔨 方式二：从源码构建

### 1. 克隆仓库

```bash
git clone https://github.com/ldzhhxx/Hermes_offline_v2.git
cd Hermes_offline_v2
```

### 2. 构建镜像

```bash
docker build -t hermes-offline:latest .
```

### 3. 运行容器（同上）

---

## 🐳 方式三：使用 Docker Compose（推荐）

### 1. 创建 `.env` 文件

```bash
cp .env.docker.example .env
```

编辑 `.env` 文件：

```env
# API密钥 - 用于保护Agent API
API_SERVER_KEY=your-secure-api-key-change-me

# WebUI密码（可选，建议设置）
HERMES_WEBUI_PASSWORD=your-webui-password

# 允许所有用户访问（开发环境）
GATEWAY_ALLOW_ALL_USERS=true

# OpenAI配置
OPENAI_API_KEY=sk-your-openai-key
OPENAI_BASE_URL=https://api.openai.com/v1

# Anthropic配置
ANTHROPIC_API_KEY=sk-ant-your-anthropic-key

# Telegram配置（可选）
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_ALLOWED_USERS=user_id1,user_id2

# Discord配置（可选）
DISCORD_BOT_TOKEN=your-discord-token
DISCORD_ALLOWED_USERS=user_id1,user_id2

# Slack配置（可选）
SLACK_BOT_TOKEN=xoxb-your-slack-token
SLACK_SIGNING_SECRET=your-signing-secret
```

### 2. 启动服务

如果已经构建了镜像：
```bash
docker run -d \
  --name hermes-offline \
  -p 18789:18789 \
  -p 5000:5000 \
  -v $(pwd)/data:/home/hermes/.hermes \
  -v $(pwd)/workspace:/home/hermes/workspace \
  --env-file .env \
  hermes-offline:latest
```

或者使用原项目的 docker-compose.yml（需要安装 docker-compose）：
```bash
docker-compose up -d
```

### 3. 查看日志

```bash
docker logs -f hermes-offline
```

---

## 🔐 安全配置

### 生产环境建议

1. **设置强密码**
   ```env
   HERMES_WEBUI_PASSWORD=your-strong-password-here
   ```

2. **限制用户访问**
   ```env
   GATEWAY_ALLOW_ALL_USERS=false
   TELEGRAM_ALLOWED_USERS=your_telegram_id
   ```

3. **使用反向代理**
   ```nginx
   # Nginx配置示例
   server {
       listen 80;
       server_name your-domain.com;
       
       location / {
           proxy_pass http://localhost:18789;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
           
           # WebSocket支持
           proxy_http_version 1.1;
           proxy_set_header Upgrade $http_upgrade;
           proxy_set_header Connection "upgrade";
       }
   }
   ```

4. **启用HTTPS**（使用 Let's Encrypt）
   ```bash
   sudo certbot --nginx -d your-domain.com
   ```

---

## 🛠️ 常用命令

### 容器管理

```bash
# 查看运行状态
docker ps

# 查看日志
docker logs -f hermes-offline

# 进入容器
docker exec -it hermes-offline bash

# 重启容器
docker restart hermes-offline

# 停止容器
docker stop hermes-offline

# 启动容器
docker start hermes-offline

# 删除容器
docker rm -f hermes-offline
```

### 数据备份

```bash
# 备份数据目录
tar -czf hermes-backup-$(date +%Y%m%d).tar.gz data/ workspace/

# 恢复数据
tar -xzf hermes-backup-YYYYMMDD.tar.gz
```

### 更新镜像

```bash
# 停止并删除旧容器
docker stop hermes-offline
docker rm hermes-offline

# 重新构建镜像
docker build -t hermes-offline:latest .

# 启动新容器（数据会保留在挂载的目录中）
docker run -d \
  --name hermes-offline \
  -p 18789:18789 \
  -p 5000:5000 \
  -v $(pwd)/data:/home/hermes/.hermes \
  -v $(pwd)/workspace:/home/hermes/workspace \
  --env-file .env \
  hermes-offline:latest
```

---

## 📊 监控和维护

### 健康检查

```bash
# 检查Agent API
curl http://localhost:5000/health

# 检查WebUI
curl -I http://localhost:18789
```

### 资源监控

```bash
# 查看容器资源使用
docker stats hermes-offline

# 查看容器详细信息
docker inspect hermes-offline
```

### 日志管理

```bash
# 查看最新100行日志
docker logs --tail 100 hermes-offline

# 查看实时日志
docker logs -f hermes-offline

# 查看特定时间的日志
docker logs --since "2024-01-01T00:00:00" hermes-offline
```

---

## ❓ 故障排查

### 容器无法启动

1. 检查端口是否被占用：
   ```bash
   netstat -tulpn | grep -E '18789|5000'
   ```

2. 检查日志：
   ```bash
   docker logs hermes-offline
   ```

3. 检查文件权限：
   ```bash
   ls -la data/ workspace/
   ```

### WebUI无法访问

1. 确认容器正在运行：
   ```bash
   docker ps | grep hermes-offline
   ```

2. 检查防火墙规则：
   ```bash
   # Ubuntu/Debian
   sudo ufw status
   sudo ufw allow 18789
   
   # CentOS/RHEL
   sudo firewall-cmd --list-ports
   sudo firewall-cmd --add-port=18789/tcp --permanent
   sudo firewall-cmd --reload
   ```

3. 测试本地连接：
   ```bash
   curl http://localhost:18789
   ```

### API连接错误

检查环境变量配置：
```bash
docker exec hermes-offline env | grep -E 'API|OPENAI|ANTHROPIC'
```

---

## 🌐 云服务器部署提示

### AWS EC2
- 在安全组中开放 18789 和 5000 端口
- 使用弹性IP获得固定公网地址

### 阿里云 ECS
- 在安全组规则中添加入方向规则
- 协议类型：自定义TCP
- 端口范围：18789,5000

### 腾讯云 CVM
- 在防火墙规则中添加放通规则
- 应用类型：自定义
- 端口：18789,5000

### DigitalOcean Droplet
- 在Cloud Firewall中创建入站规则
- 或使用ufw管理本地防火墙

---

## 📞 支持

如有问题，请查看：
- GitHub仓库：https://github.com/ldzhhxx/Hermes_offline_v2
- 查看容器日志获取详细错误信息
- 确保Docker版本 >= 20.10

---

**部署完成后，你就可以通过浏览器访问 http://your-server-ip:18789 使用Hermes了！** 🎉
