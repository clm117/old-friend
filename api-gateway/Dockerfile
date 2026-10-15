# syntax=docker/dockerfile:1
# ============================================================
# 语伴 接入层（API Gateway）镜像
# 构建上下文：项目根目录（context: .）
#   docker build -f api-gateway/Dockerfile -t yuban-gateway .
# ============================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先装依赖，充分利用层缓存（依赖不变时后续改代码不会重装）
COPY api-gateway/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝网关代码
COPY api-gateway/ ./

EXPOSE 8005

# 生产：单进程 uvicorn。WebSocket 代理场景下不宜多 worker（会话粘性）
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8005"]
