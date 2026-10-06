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
# ⚠ 前端鉴权（整改 P2 / PRD §9）：看板必须在构建期注入 VITE_API_KEY，
# 否则带 X-API-Key 头的请求会因缺 key 被后端 403。云端重建时通过
#   docker compose build --build-arg VITE_API_KEY=<与 INTEGRATION_API_KEY 一致>
# 透传；不传则留空（此时看板所有 API 会 403，需补配）。
ARG VITE_API_KEY=""
ENV VITE_API_KEY=$VITE_API_KEY
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
#
# ★ 2026-10-06 实测补齐（此前本层只装 scrapling 库、不装浏览器，导致
#   dce_scrapling.available()=True 但一跑就 TargetClosedError / 无浏览器）：
#   该层现在把「浏览器 + 全部系统库 + LD_LIBRARY_PATH」一并装好：
#   1) 系统库：slim 镜像缺 25 个 .so（libnss3/libgbm/libasound…），
#      chromium 起来就报 "error while loading shared libraries"。
#      必须用 root 装，且 --no-install-recommends 以控制体积。
#      apt 源：官方 deb.debian.org 在部分云端极慢，故默认走清华镜像，
#      可用 --build-arg APT_MIRROR=... 覆盖。
#   2) 浏览器：走 npmmirror 的 playwright 镜像（官方 cdn.playwright.dev
#      实测仅 ~0.5KB/s，会卡死在 0%；镜像源 186MB 秒级完成）。
#      装到 PLAYWRIGHT_BROWSERS_PATH 指定的共享目录，appuser 亦可读。
#   3) 运行时：ENV LD_LIBRARY_PATH 让 chromium 能找到上面装的库。
ARG WITH_SCRAPLING=0
# Debian 镜像（国内云端建议保持清华；可传 APT_MIRROR 覆盖）
ARG APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian
# playwright 浏览器下载源（官方 CDN 在本云端不可用，必须镜像）
ARG PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
# 浏览器安装位置（需在 USER appuser 之前 chown）
ARG PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

RUN if [ "$WITH_SCRAPLING" = "1" ]; then \
        set -eux; \
        pip install "scrapling[fetchers]" -i ${PIP_INDEX_URL} ; \
        # ---- 1) 系统库（chromium 运行时依赖，缺一即起不来）----
        printf 'deb %s trixie main contrib non-free non-free-firmware\n' "${APT_MIRROR}" > /etc/apt/sources.list ; \
        apt-get update ; \
        apt-get install -y --no-install-recommends \
            libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
            libdbus-1-3 libcups2 libexpat1 libxcb1 libxkbcommon0 libasound2 \
            libgbm1 libx11-6 libxext6 libcairo2 libpango-1.0-0 libxcomposite1 \
            libxdamage1 libxfixes3 libxrandr2 libatspi2.0-0 libdrm2 libxshmfence1 \
            libxi6 libxrender1 libavahi-client3 libavahi-common3 libfontconfig1 \
            libfreetype6 libfribidi0 libharfbuzz0b libpixman-1-0 libpng16-16 \
            libthai0 libxcb-render0 libxcb-shm0 libxau6 libxdmcp6 \
            fonts-liberation ; \
        rm -rf /var/lib/apt/lists/* ; \
        # ---- 2) 浏览器二进制（npmmirror 源）----
        PLAYWRIGHT_DOWNLOAD_HOST="${PLAYWRIGHT_DOWNLOAD_HOST}" \
        PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH}" \
        patchright install chromium || python -m patchright install chromium ; \
        mkdir -p "${PLAYWRIGHT_BROWSERS_PATH}" ; \
        echo "[build] chromium 已安装到 ${PLAYWRIGHT_BROWSERS_PATH}" ; \
    else \
        echo "[build] 跳过 Scrapling 层（WITH_SCRAPLING=0）：DCE 由宿主机采集器供给" ; \
    fi

# 运行时环境：chromium 需要这些库 + 浏览器路径
# （ENV 对 WITH_SCRAPLING=0 也无害：目录不存在时 dce_scrapling.pick_browser()
#   返回 None，StealthyFetcher 会走自己的解析逻辑）
ENV PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH} \
    LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu

# 源码
COPY app ./app
COPY scripts ./scripts
COPY config ./config
# DB 迁移脚本：与代码同版本入库，远端部署后可直接在容器内执行
#   python /app/scripts/db_apply_migrations.py --apply
COPY migrations ./migrations
# 验收测试：随镜像入库，远端容器可直接 `python -m pytest tests/` 复跑
# （T18 双向等价回归 / T16 前视守卫 / V4 数据自检 / V5 组合熔断）
COPY tests ./tests
# M5 看板静态文件（Stage 1 构建产物）
COPY --from=webbuilder /build/dist ./web/dist
# M8 决策链路源码（WB skill 的内化副本）
#   由 `python scripts/vendor_pipeline_src.py` 生成到 vendor/pipeline_src。
#   进镜像 = 远端部署不再依赖任何宿主机 bind mount（app/pipeline/config.py 的
#   src 默认值正是 /app/pipeline_src，无需额外配置）。
COPY vendor/pipeline_src ./pipeline_src

# 运行用户（非 root）
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
# ★预建运行时目录并授权（2026-10-06）
#   /app/logs、/app/runtime 在 compose 里是 **named volume**，docker 只会「首次
#   创建」时从镜像内容初始化属主；若镜像内不存在该目录，卷会被建成 root:root，
#   容器内 appuser(uid 10001) 写不进去 → 启动即崩：
#     PermissionError: [Errno 13] Permission denied: '/app/logs/qhyc.log'
#   （表现为 api/scheduler 无限重启，healthcheck 永不通过。）
#   这里在镜像内先建好并 chown，卷初始化即可继承正确属主。
RUN mkdir -p /app/logs /app/runtime && chown -R appuser:appuser /app/logs /app/runtime
# 浏览器目录归 appuser（仅 WITH_SCRAPLING=1 时存在；不存在时此段是 no-op）
RUN if [ -d "${PLAYWRIGHT_BROWSERS_PATH}" ]; then \
        chown -R appuser:appuser "${PLAYWRIGHT_BROWSERS_PATH}" ; \
    fi
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# 默认启动 FastAPI（容器内另起调度进程，二者共享同一镜像）
CMD ["python", "-m", "app.main"]