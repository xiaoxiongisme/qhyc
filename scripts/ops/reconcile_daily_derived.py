# -*- coding: utf-8 -*-
"""日频派生链收盘后收敛（CL-20261118-01）。

背景（2026-11-18 实证）
------------------------
``spot_basis_daily``(17:10) 采集 akshare 现货/基差。当日现货数据**可能晚于
17:10 才发布**：11-18 那轮返回 150 行（3 个交易日×50 品种）却完全没有
11-18 的行，但 ``fetch_spot_basis`` 对空返回只 warning + ``return []``，
``_spot_basis_job`` 以「函数没抛异常」记 success。于是：

    spot_basis(11-17) → main_contract_map(11-17) → dim_main_contract_inferred(11-17)

三张表一起停在 T-1，而 ``DATA-MAIN-CONTRACT-INFER-FRESH`` 只比较 inferred
与 map 的**相对**日期，两表同步落后时仍 PASS（09:10 实测全绿），直到 21:00
``settle_cutoff_hour`` 切换期望交易日才由表级新鲜度巡检发现。

本作业放在 **20:30**（早于 21:00 告警阈值、晚于 18:30 反推作业），做三件事：

  ① 若 spot_basis 当日行数不足 → 用 ``require_today=True`` 重采（幂等 UPSERT）；
  ② 若 main_contract_map 当日行数不足 → 由 spot_basis 重新派生；
  ③ 若 dim_main_contract_inferred 当日行数不足 → 用 888 分钟序列重新反推。

任一环节仍缺当日行 → **抛异常**（不静默），让 task_runner 记 failed 并触发
告警。全程幂等：已齐的交易日直接跳过，可安全重复运行 / 手动补跑。

用法（容器内，由宿主机 task_runner 驱动）::

    docker exec -w /app -e PYTHONPATH=/app qhyc-api \
        python scripts/ops/reconcile_daily_derived.py

可选 ``--date YYYY-MM-DD`` 指定交易日（默认 Asia/Shanghai 当日），``--dry-run``
只诊断不写库。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "app").is_dir())
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from app.core.db import session_scope  # noqa: E402
from app.core.logging import logger  # noqa: E402
from app.ingest.spot_basis import collect_spot_basis  # noqa: E402

CST = ZoneInfo("Asia/Shanghai")
#: 正常每交易日 50 个品种；低于此阈值视为「当日未完整产出」。
#: 取 30 而非 50，兼容个别品种被 blocked_varieties 过滤的合法情形。
MIN_ROWS = 30
#: 重采窗口：覆盖最近 6 个自然日，足以含当日与前一交易日。
LOOKBACK_DAYS = 6

TABLES = {
    "spot_basis": ("l0_raw.spot_basis", "report_date"),
    "main_contract_map": ("l3_ref.main_contract_map", "trade_date"),
    "inferred": ("l3_ref.dim_main_contract_inferred", "trade_date"),
}


def _today_rows(session, d: date) -> dict[str, int]:
    out = {}
    for key, (tbl, col) in TABLES.items():
        q = f"SELECT count(*) FROM {tbl} WHERE {col} = :d"
        if key == "inferred":
            q += " AND inferred_symbol IS NOT NULL"
        out[key] = int(session.execute(text(q), {"d": d}).scalar() or 0)
    return out


def _is_trade_day(session, d: date) -> bool:
    """交易日真源 futures_rule（禁止 weekday 猜测，CL-20261107-18）。"""
    return bool(session.execute(
        text("SELECT EXISTS(SELECT 1 FROM futures_rule WHERE trade_date = :d)"),
        {"d": d}).scalar())


def _run_cli(rel: str, *args: str) -> None:
    """调用已验证的写入脚本；check=True 保证 returncode 不被吞（skill 坑 #9）。"""
    cmd = [sys.executable, str(ROOT / rel), *args]
    logger.info("[reconcile] run %s", " ".join(cmd))
    subprocess.run(cmd, cwd=str(ROOT), check=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", type=date.fromisoformat, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    d = args.date or datetime.now(CST).date()
    with session_scope() as s:
        if not _is_trade_day(s, d):
            print(f"{d} 非交易日（futures_rule 真源）→ 无需收敛")
            return 0

        before = _today_rows(s, d)
        print(f"[{d}] 收敛前: {before}")
        if all(v >= MIN_ROWS for v in before.values()):
            print(f"[{d}] 三表当日均已齐（阈值 {MIN_ROWS}）→ 无事可做")
            return 0
        if args.dry_run:
            print(f"[{d}] DRY-RUN 未写库；缺口: "
                  f"{[k for k, v in before.items() if v < MIN_ROWS]}")
            return 1

        # ① 上游：重采现货/基差（当日缺行会抛 RuntimeError，绝不静默 success）
        if before["spot_basis"] < MIN_ROWS:
            stats = collect_spot_basis(
                s, d - timedelta(days=LOOKBACK_DAYS), d,
                products=None, require_today=True, min_today_rows=MIN_ROWS)
            print(f"[{d}] ① spot_basis 重采: {stats}")

        # ② 由 spot_basis.dominant_contract 派生主力映射（幂等 UPSERT）
        if before["main_contract_map"] < MIN_ROWS:
            _run_cli("scripts/refresh_main_contract_map.py", "--since", d.isoformat())
            print(f"[{d}] ② main_contract_map 已派生")

        # ③ 由 888 分钟序列反推真实合约（--apply 才写库）
        if before["inferred"] < MIN_ROWS:
            _run_cli("scripts/ops/infer_main_contract.py",
                     "--date", d.isoformat(), "--apply")
            print(f"[{d}] ③ dim_main_contract_inferred 已反推")

    # 用独立 session 复核，避免 SQLAlchemy 身份映射 / 未提交事务造成假绿
    with session_scope() as s2:
        after = _today_rows(s2, d)
    print(f"[{d}] 收敛后: {after}")
    missing = [k for k, v in after.items() if v < MIN_ROWS]
    if missing:
        raise RuntimeError(
            f"[reconcile] {d} 收敛后仍缺当日行（阈值 {MIN_ROWS}）: {missing} / {after}；"
            "上游可能真断供，禁止记为成功（CL-20261118-01）")
    print(f"[{d}] OK 三表当日齐全")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
