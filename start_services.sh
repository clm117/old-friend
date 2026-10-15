#!/bin/bash

# 语音交互APP启动脚本

# 检查Python环境
echo "检查Python环境..."
python --version

# 检查Redis是否运行
echo "检查Redis服务..."
if command -v redis-server &> /dev/null; then
    echo "Redis已安装"
else
    echo "Redis未安装，请先安装Redis"
    exit 1
fi

# 启动Redis（如果未运行）
echo "启动Redis服务..."
if ! ps aux | grep -q "[r]edis-server"; then
    redis-server &
    echo "Redis服务已启动"
else
    echo "Redis服务已在运行"
fi

# 进入API Gateway目录并启动服务
echo "启动API Gateway..."
cd api-gateway
python app.py &
sleep 2

# 进入AI服务目录并启动服务
echo "启动核心AI服务..."
cd ../ai-service
python main.py &
sleep 5

echo "所有服务已启动！"
echo "- API Gateway: http://localhost:8000"
echo "- 核心AI服务: http://localhost:8080"
echo "- Redis: localhost:6379"

# 查看服务状态
echo "\n服务状态："
ps aux | grep -E "(python|redis-server)" | grep -v grep