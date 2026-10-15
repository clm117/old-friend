# syntax=docker/dockerfile:1
# ============================================================
# 语伴 核心 AI 服务镜像（GPU / CUDA）
# 构建上下文：项目根目录（context: .）
#   docker build -f ai-service/Dockerfile -t yuban-ai-service .
#
# 关键约定：模型路径在代码中按「项目根/models」解析，
#   因此代码必须落在 /app/ai-service/，模型挂载到 /app/models。
#
# 基础镜像：默认与开发环境 torch 版本对齐（CUDA 13）。
#   若目标服务器驱动较旧，可覆盖：
#   docker build --build-arg BASE_IMAGE=pytorch/pytorch:2.5.1-cuda12.1-cudnn9-runtime ...
# ============================================================
ARG BASE_IMAGE=pytorch/pytorch:2.11.0-cuda13.0-cudnn9-runtime
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/app/.cache/huggingface \
    TORCH_HOME=/app/.cache/torch

# 系统依赖：音频编解码 + torch.compile 需要的编译工具链
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libsndfile1 \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/ai-service

# 先装依赖（torch 由基础镜像提供，不重复安装）
COPY ai-service/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝 AI 服务代码（保持 ai-service/ 这一层目录结构）
COPY ai-service/ /app/ai-service/

EXPOSE 8084

# 注意：main.py 在 import 阶段即加载模型，启动较慢（分钟级），
#       健康检查需配置足够的 start_period。
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8084"]
