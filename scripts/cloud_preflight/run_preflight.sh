#!/usr/bin/env bash
# 云端预检编排：在 STAGING 克隆库上跑 007/008/009 应用 + 校验 + L2 A/B + L1 夹具。
#
# 用法（先配 PG 环境变量或 --dsn）：
#   export PGHOST=... PGPORT=5432 PGUSER=... PGPASSWORD=... PGDATABASE=qhyc_staging
#   bash scripts/cloud_preflight/run_preflight.sh synthetic
#   bash scripts/cloud_preflight/run_preflight.sh real --symbol RB888 --freqs min15,min30,min60
#
# 任何一步失败（ON_ERROR_STOP）立即停止，绝不继续写库。
set -u
REPO=$(cd "$(dirname "$0")/../.." && pwd)
MODE="${1:-synthetic}"
shift || true
ARGS=("$@")

PSQL="psql"

echo "== [1/4] 应用 007/008/009 =="
"$PSQL" -v ON_ERROR_STOP=1 -f "$REPO/migrations/007_dim_tables.sql" || exit 1
"$PSQL" -v ON_ERROR_STOP=1 -f "$REPO/migrations/008_cfg_tables.sql" || exit 1
"$PSQL" -v ON_ERROR_STOP=1 -f "$REPO/migrations/009_l0_l1_l2_stored_procs.sql" || exit 1

echo "== [2/4] 字典/配置校验 =="
"$PSQL" -v ON_ERROR_STOP=1 -f "$REPO/scripts/cloud_preflight/01_validate_dict_cfg.sql" || exit 1

echo "== [3/4] L2 A/B 对拍（sp vs Python）=="
python "$REPO/scripts/cloud_preflight/02_fixture_and_roll_ab.py" --mode "$MODE" "${ARGS[@]}" || exit 1

echo "== [4/4] L1 聚合夹具（捕获 oi 列名 bug）=="
"$PSQL" -v ON_ERROR_STOP=1 -f "$REPO/scripts/cloud_preflight/03_sp_l1_fixture.sql" || exit 1

echo "ALL PREFLIGHT CHECKS PASSED（仅已知分歧，见 02 输出）"
