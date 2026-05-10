#!/bin/bash

# Hermes Offline v2 快速启动脚本
# 使用方法: ./quick-start.sh

set -e

echo "🚀 Hermes Offline v2 快速启动脚本"
echo "=================================="
echo ""

# 检查Docker是否安装
if ! command -v docker &> /dev/null; then
    echo "❌ 错误：未检测到Docker，请先安装Docker"
    echo "   Ubuntu/Debian: sudo apt-get install docker.io"
    echo "   CentOS/RHEL: sudo yum install docker"
    echo "   或访问: https://docs.docker.com/get-docker/"
    exit 1
fi

echo "✅ Docker已安装: $(docker --version)"

# 检查Docker镜像
if docker images | grep -q "hermes-offline"; then
    echo "✅ 找到 hermes-offline 镜像"
else
    echo "⚠️  未找到镜像，开始构建..."
    if [ -f "Dockerfile" ]; then
        docker build -t hermes-offline:latest .
    elif [ -f "hermes-offline-image.tar" ]; then
        echo "📦 从tar文件加载镜像..."
        docker load -i hermes-offline-image.tar
    else
        echo "❌ 错误：找不到Dockerfile或镜像文件"
        exit 1
    fi
fi

# 创建必要的目录
echo ""
echo "📁 创建数据目录..."
mkdir -p data workspace
echo "✅ 目录已创建: $(pwd)/data 和 $(pwd)/workspace"

# 检查是否已有运行的容器
if docker ps -a --format '{{.Names}}' | grep -q "^hermes-offline$"; then
    echo ""
    echo "⚠️  检测到已存在的 hermes-offline 容器"
    read -p "是否删除并重新创建? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "🗑️  删除旧容器..."
        docker rm -f hermes-offline
    else
        echo "尝试启动现有容器..."
        docker start hermes-offline
        echo ""
        echo "✅ 容器已启动！"
        docker logs --tail 20 hermes-offline
        exit 0
    fi
fi

# 环境变量配置
echo ""
echo "🔧 配置环境变量..."

# 生成随机API密钥
API_KEY="hermes-$(openssl rand -hex 16 2>/dev/null || echo "offline-default-key-$(date +%s)")"

# 询问是否设置WebUI密码
echo ""
read -p "是否设置WebUI访问密码? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    read -p "请输入密码: " WEBUI_PASSWORD
    WEBUI_PASSWORD_ENV="-e HERMES_WEBUI_PASSWORD=$WEBUI_PASSWORD"
else
    WEBUI_PASSWORD_ENV=""
    echo "⚠️  警告：未设置密码，任何人都可以访问WebUI"
fi

# 启动容器
echo ""
echo "🐳 启动Docker容器..."
docker run -d \
  --name hermes-offline \
  -p 18789:18789 \
  -p 5000:5000 \
  -v "$(pwd)/data:/home/hermes/.hermes" \
  -v "$(pwd)/workspace:/home/hermes/workspace" \
  -e API_SERVER_KEY="$API_KEY" \
  -e GATEWAY_ALLOW_ALL_USERS=true \
  $WEBUI_PASSWORD_ENV \
  --restart unless-stopped \
  hermes-offline:latest

# 等待服务启动
echo ""
echo "⏳ 等待服务启动..."
sleep 5

# 检查容器状态
if docker ps | grep -q "hermes-offline"; then
    echo ""
    echo "✅ =================================="
    echo "✅ Hermes Offline 启动成功！"
    echo "✅ =================================="
    echo ""
    echo "📍 访问地址："
    echo "   WebUI:    http://localhost:18789"
    echo "   API健康:  http://localhost:5000/health"
    echo ""
    echo "🔑 API密钥: $API_KEY"
    echo "   (请妥善保存此密钥)"
    echo ""
    
    # 尝试获取本机IP
    if command -v hostname &> /dev/null; then
        LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
        if [ -n "$LOCAL_IP" ]; then
            echo "🌐 局域网访问: http://$LOCAL_IP:18789"
            echo ""
        fi
    fi
    
    echo "📋 常用命令："
    echo "   查看日志: docker logs -f hermes-offline"
    echo "   停止服务: docker stop hermes-offline"
    echo "   启动服务: docker start hermes-offline"
    echo "   重启服务: docker restart hermes-offline"
    echo ""
    echo "📖 详细文档: 请查看 DEPLOYMENT_GUIDE.md"
    echo ""
    
    # 显示最近的日志
    echo "📜 最近的日志："
    echo "=================================="
    docker logs --tail 15 hermes-offline
    
else
    echo ""
    echo "❌ 容器启动失败，请查看日志："
    docker logs hermes-offline
    exit 1
fi
