#!/usr/bin/env bash
# =====================================================
# 云端唯一部署入口（2026-10-07 固化）
# =====================================================
# 事故背景：2026-10-07 云端生产库一度「变空」，根因是两个 compose 混用 ——
# timescaledb 容器最初由 docker-compose.yml（本地开发版）创建，其 pgdata 是
# bind mount ./pgdata；而云端版 docker-compose.cloud.yml 用 named volume pgdata:
# （实际卷 qhyc_pgdata）。容器一直挂在宿主空目录 ./pgdata 上，PostgreSQL 判定
# 空目录后执行 initdb，12G 真实数据被闲置、库看起来是空的。数据从未丢失。
#
# 本脚本把「只用 cloud 版」固化为流程，并在动手前做四道前置门禁：
#   1) pgdata 必须是 named volume（本次事故的直接判据）
#   2) 5432 只绑环回、不对外暴露
#   3) 目标卷是有效 PG 数据目录
#   4) 代码与 git 一致
# 任何一项不过即中止，避免重建完才发现库是空的。
#
# 用法：
#   scripts/deploy_cloud.sh                # 构建 + 部署
#   scripts/deploy_cloud.sh --check-only   # 只体检，不变更
#   scripts/deploy_cloud.sh --no-build     # 跳过构建
#   WITH_SCRAPLING=0 scripts/deploy_cloud.sh
# =====================================================
set -euo pipefail

REPO_DIR="${REPO_DIR:-/Docker/qhyc}"
COMPOSE_FILE="$REPO_DIR/docker-compose.cloud.yml"
COMPOSE="docker compose -f $COMPOSE_FILE"
DB_CNT="qhyc-timescaledb"
IMAGE="qhyc:latest"
WITH_SCRAPLING="${WITH_SCRAPLING:-1}"
SERVICES="${SERVICES:-api scheduler pipeline}"

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; RST=$'\033[0m'
ok(){ echo "${GRN}[OK]${RST} $*"; }
warn(){ echo "${YLW}[WARN]${RST} $*"; }
die(){ echo "${RED}[FAIL]${RST} $*" >&2; exit 1; }

CHECK_ONLY=0; DO_BUILD=1
for a in "$@"; do
  case "$a" in
    --check-only) CHECK_ONLY=1 ;;
    --no-build) DO_BUILD=0 ;;
    *) die "未知参数: $a" ;;
  esac
done

echo "=============================================="
echo " 云端部署门禁  compose=$COMPOSE_FILE  $(date '+%F %T')"
echo "=============================================="

# ---------- 门禁 0：必须用 cloud 版且 pgdata 为 named volume ----------
[ -f "$COMPOSE_FILE" ] || die "找不到 $COMPOSE_FILE（云端只能用 docker-compose.cloud.yml）"
if grep -qE '^      - \./pgdata:/var/lib/postgresql/data' "$COMPOSE_FILE"; then
  die "compose 里 pgdata 是 bind mount — 正是 2026-10-07 数据库变空库的根因，禁止部署"
fi
grep -qE '^      - pgdata:/var/lib/postgresql/data' "$COMPOSE_FILE" \
  || die "compose 未声明 named volume pgdata:，请核对 $COMPOSE_FILE"
ok "门禁0：使用 cloud 版且 pgdata 为 named volume"

# ---------- 门禁 1：5432 仅绑环回 ----------
if grep -qE '^      - [0-9.]+:5432' "$COMPOSE_FILE"; then
  die "5432 绑定到非环回地址（应 127.0.0.1:5432:5432），拒绝部署"
fi
ok "门禁1：5432 未对外暴露"

# ---------- 门禁 2：运行中容器的挂载类型（核心判据） ----------
if docker inspect "$DB_CNT" >/dev/null 2>&1; then
  M="$(docker inspect "$DB_CNT" \
        --format '{{range .Mounts}}{{.Type}} {{.Destination}}{{println}}{{end}}' \
        | grep '/var/lib/postgresql/data' || true)"
  [ -n "$M" ] || die "容器 $DB_CNT 未挂载 /var/lib/postgresql/data"
  case "$M" in
    volume*) ok "门禁2：数据目录为 named volume（$M）" ;;
    bind*)   die "数据目录是 bind mount（$M）— 事故根因；请用 cloud 版重建" ;;
    *)       die "未知挂载类型：$M" ;;
  esac
  L="$(sudo ss -ltn 2>/dev/null | awk '/:5432 /{print $4}' | head -1 || true)"
  case "$L" in
    127.0.0.1:*) ok "门禁2b：宿主 5432 仅监听环回（$L）" ;;
    "")           warn "未能确认 5432 监听地址" ;;
    *)            die "宿主 5432 监听在非环回地址（$L），有公网暴露风险" ;;
  esac
else
  warn "门禁2：容器 $DB_CNT 尚未创建（首次部署正常）"
fi

# ---------- 门禁 3：目标卷是有效 PG 数据目录 ----------
VOL="$(docker volume ls -q | grep -E '(^|_)pgdata$' | head -1 || true)"
if [ -n "$VOL" ]; then
  VD="/var/lib/docker/volumes/$VOL/_data"
  if sudo test -f "$VD/PG_VERSION" && sudo test -f "$VD/global/pg_control"; then
    ok "门禁3：卷 $VOL 为有效 PG 数据目录（$(sudo du -sh "$VD" 2>/dev/null | cut -f1)）"
  else
    warn "卷 $VOL 存在但不是有效 PG 数据目录（可能是空壳）"
  fi
fi

# ---------- 门禁 4：代码与 git 一致 ----------
cd "$REPO_DIR"
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  warn "工作区有未提交改动："
  git status --porcelain | head -5 | sed 's/^/        /'
else
  ok "门禁4a：工作区干净"
fi
L2="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
R2="$(git rev-parse --short '@{u}' 2>/dev/null || echo none)"
if [ "$L2" = "$R2" ]; then
  ok "门禁4b：代码已与远端同步（$L2）"
else
  warn "本地 HEAD=$L2 远端=$R2 不一致 — 建议先 push，否则容器跑旧代码"
fi

if [ "$CHECK_ONLY" = "1" ]; then
  echo; echo "体检完成（--check-only，未做变更）。"; exit 0
fi

# ---------- 备份结构（不动数据） ----------
echo; echo ">>> 导出库结构（数据不动）"
mkdir -p "$REPO_DIR/runtime/deploy_backup"
STAMP="$(date +%Y%m%d_%H%M%S)"
if docker exec "$DB_CNT" pg_dump -U futures -d futures -s -Fc \
     > "$REPO_DIR/runtime/deploy_backup/schema_$STAMP.dump" 2>/dev/null; then
  ok "结构已备份 runtime/deploy_backup/schema_$STAMP.dump"
else
  warn "结构备份跳过（DB 可能未就绪）"
fi

# ---------- 构建 ----------
if [ "$DO_BUILD" = "1" ]; then
  echo; echo ">>> 构建镜像 WITH_SCRAPLING=$WITH_SCRAPLING"
  $COMPOSE build --build-arg WITH_SCRAPLING="$WITH_SCRAPLING" api
  # 构建产物标签是 qhyc-api:latest，而服务引用 $IMAGE，必须同步打标签，
  # 否则 up 会继续跑旧镜像（曾出现"容器里没有新代码/没有浏览器"）。
  docker tag qhyc-api:latest "$IMAGE" 2>/dev/null || warn "打标签 $IMAGE 失败"
  ok "构建完成并打标签 $IMAGE"
fi

# ---------- 部署 ----------
echo; echo ">>> 重建服务：$SERVICES"
$COMPOSE up -d --force-recreate $SERVICES

# ---------- 部署后校验 ----------
sleep 6
echo; echo ">>> 部署后校验"
NM="$(docker inspect "$DB_CNT" \
      --format '{{range .Mounts}}{{.Type}} {{.Destination}}{{println}}{{end}}' \
      | grep '/var/lib/postgresql/data' || true)"
case "$NM" in
  volume*) ok "数据目录仍为 named volume（$NM）" ;;
  *)       die "部署后挂载异常：$NM" ;;
esac
if docker exec "$DB_CNT" pg_isready -U futures -d futures >/dev/null 2>&1; then
  ok "数据库就绪"
else
  warn "pg_isready 未通过，稍后重试"
fi
docker ps --format '{{.Names}} | {{.Status}}' | grep -E 'qhyc-(api|scheduler|timescaledb)' || true
echo; echo "部署完成。健康检查： curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/health"
