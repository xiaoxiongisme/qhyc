# =====================================================
# 期货预测平台 - Python API 镜像（含 APScheduler）
# 基础镜像：python:3.11-slim（兼容 akshare/tqsdk）
# =====================================================
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

# 时区与系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata \
        curl \
        ca-certificates \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖先行（缓存层）
# 使用清华 PyPI 镜像（国内网络）；云端构建如需官方源可传 --build-arg PIP_INDEX_URL
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
COPY requirements.txt .
RUN pip install -r requirements.txt -i ${PIP_INDEX_URL}

# LSTM（torch CPU 版，可选层）：拉取失败不阻塞构建（LSTM 模型自动跳过）
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
    || echo "[build] torch install failed, LSTM disabled"

# 源码
COPY app ./app
COPY scripts ./scripts
COPY config ./config

# 运行用户（非 root）
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# 默认启动 FastAPI（容器内另起调度进程，二者共享同一镜像）
CMD ["python", "-m", "app.main"]