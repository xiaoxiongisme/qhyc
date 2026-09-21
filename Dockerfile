# =====================================================
# 期货预测平台 - 多阶段构建
# Stage 1: node 构建 React 看板（M5）
# Stage 2: python API（含静态托管看板）
# =====================================================

# ---------- Stage 1: 看板构建 ----------
# 说明：前端默认在宿主机本地构建（node 环境齐全），再由 docker-compose
# 把 ./web/dist 挂载进容器。此阶段仅作为“干净从零构建”的兜底。
FROM node:20-alpine AS webbuilder
WORKDIR /build
# 依赖先行（缓存层）；国内网络走 npmmirror，可用 --build-arg 覆盖
ARG NPM_REGISTRY=https://registry.npmmirror.com
COPY web/package.json ./
# 不复制 Windows 生成的 package-lock.json：否则 npm 会据此生成指向
# node.exe 的 bin 链接，在 alpine 下报 “node.exe: not found”
RUN npm config set registry ${NPM_REGISTRY} && npm install --no-package-lock
COPY web/ ./
RUN npm run build

# ---------- Stage 2: Python API ----------
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

# DCE 龙虎榜（可选层）：大商所官网套了瑞数动态防护，纯 HTTP 一律 412，
# 必须真浏览器执行 JS → 用 Scrapling（app/ingest/dce_scrapling.py）。
# 默认关闭：该层会下载 Chromium（数百 MB），常规构建不需要。
# 需要「容器内自持 DCE 采集」时启用：
#     docker compose build --build-arg WITH_SCRAPLING=1
# 不启用时 DCE 由宿主机 DCE_scrapling_crawler.py 采集（同 src，双向幂等）。
ARG WITH_SCRAPLING=0
RUN if [ "$WITH_SCRAPLING" = "1" ]; then \
        pip install "scrapling[fetchers]" -i ${PIP_INDEX_URL} \
        && (scrapling install || echo "[build] scrapling install 失败，容器内 DCE 采集将降级") ; \
    else \
        echo "[build] 跳过 Scrapling 层（WITH_SCRAPLING=0）：DCE 由宿主机采集器供给" ; \
    fi

# 源码
COPY app ./app
COPY scripts ./scripts
COPY config ./config
# M5 看板静态文件（Stage 1 构建产物）
COPY --from=webbuilder /build/dist ./web/dist

# 运行用户（非 root）
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# 默认启动 FastAPI（容器内另起调度进程，二者共享同一镜像）
CMD ["python", "-m", "app.main"]