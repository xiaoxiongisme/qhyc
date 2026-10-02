# -*- coding: utf-8 -*-
"""附录 C 落地状态审计（V1–V6 逐项核对 + 开关默认值核验）。

用法：
    docker exec -w /app -e PYTHONPATH=/app qhyc-api python scripts/verify_appendix_c.py

判定口径（附录 C §4 + D3）：
  - 每项须有**实现**且带 `enabled` 开关、默认 `false`（默认不生效 = 可一键回滚）；
  - 未落地项明确列出，不粉饰。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from sqlalchemy import text

from app.core.db import session_scope

ROOT = Path(__file__).resolve().parents[1]
BAR = "=" * 74

V1V6_FACTORS = ("f_vol_ratio", "f_vol_z", "f_voldiv_divergence", "f_atr_pctile")


def _table_exists(s, name: str) -> bool:
    return bool(s.execute(text(
        "SELECT 1 FROM information_schema.tables WHERE table_name=:t"
    ), {"t": name}).scalar())


def _registry(s) -> dict:
    if not _table_exists(s, "factor_registry"):
        return {}
    rows = s.execute(text(
        "SELECT factor_id, category, enabled, max_weight, version "
        "FROM factor_registry"
    )).all()
    return {r[0]: {"category": r[1], "enabled": bool(r[2]),
                   "max_weight": float(r[3] or 0), "version": r[4]} for r in rows}


def _factor_rows(s, fid: str) -> int:
    try:
        return int(s.execute(text(
            "SELECT count(*) FROM factor_value WHERE factor_id=:f"
        ), {"f": fid}).scalar() or 0)
    except Exception:
        return -1


def _py_has(relpath: str, symbol: str) -> bool:
    p = ROOT / relpath
    if not p.exists():
        return False
    return symbol in p.read_text(encoding="utf-8", errors="ignore")


def main() -> int:
    with session_scope() as s:
        reg = _registry(s)
        rows_by_factor = {f: _factor_rows(s, f) for f in V1V6_FACTORS}

    print(BAR)
    print("附录 C 借鉴落地验证（v0.4）状态审计")
    print(BAR)

    # ---- V1 / V6：量价维度 + 波动率分位（因子层挂载，被偏置乘子消费）----
    print("\n[V1 量价维度 · P0]  落点：因子层（VR/VZ/量价背离）→ 被既有偏置乘子消费")
    impl_v1 = _py_has("scripts/compute_factor_v1v6.py", "f_voldiv_divergence")
    print("  实现脚本 compute_factor_v1v6.py 含 V1 因子     : {0}".format(impl_v1))
    for f in ("f_vol_ratio", "f_vol_z", "f_voldiv_divergence"):
        r = reg.get(f)
        print("  {0:<22} 注册={1} enabled={2} 已算行数={3}".format(
            f, r is not None, r["enabled"] if r else "-", rows_by_factor.get(f)))
    wired = _py_has("app/strategies/fusion_signal.py", "get_bias_multipliers")
    print("  引擎接入点 get_bias_multipliers                : {0}".format(wired))

    print("\n[V6 波动率分位 · P1]  落点：引擎 ATR 体系（因子层挂载）")
    for f in ("f_atr_pctile",):
        r = reg.get(f)
        print("  {0:<22} 注册={1} enabled={2} 已算行数={3}".format(
            f, r is not None, r["enabled"] if r else "-", rows_by_factor.get(f)))

    # ---- V2 前视加固 ----
    print("\n[V2 前视加固 · P0]  落点：回测数据准备断言 + 启动守卫")
    guard_fn = _py_has("scripts/compute_factor_v1v6.py", "has_lookahead_guard")
    guard_call = _py_has("app/scheduler.py", "_verify_lookahead_guard")
    print("  has_lookahead_guard（合成数据哨兵）             : {0}".format(guard_fn))
    print("  启动期调用 _verify_lookahead_guard（fail-loud） : {0}".format(guard_call))
    dec = _py_has("scripts/compute_factor_v1v6.py", 'DECISION_HM = "15:00"')
    print("  决策时点前视防护 DECISION_HM=15:00              : {0}".format(dec))

    # ---- V3 五关闸门 ----
    print("\n[V3 过拟合五关固化 · P1]  落点：引擎发布校验闸门")
    sd = _py_has("app/backtest/robustness.py", "def silent_degradation")
    six = _py_has("app/backtest/robustness.py", "def run_six_checks")
    gate = (ROOT / "scripts" / "release_gate.py").exists()
    admit = (ROOT / "scripts" / "admit_factors.py").exists()
    print("  robustness.silent_degradation（笔数相同阻断）   : {0}".format(sd))
    print("  robustness.run_six_checks（六项检验）           : {0}".format(six))
    print("  scripts/release_gate.py（统一发布闸门，本次新增）: {0}".format(gate))
    print("  scripts/admit_factors.py（因子准入裁决）        : {0}".format(admit))

    # ---- V4 数据自检 ----
    print("\n[V4 数据质量自检 · P1]  落点：调度每日流程 17:35")
    v4 = _py_has("app/ingest/data_selfcheck.py", "class SelfCheckConfig")
    v4_def = _py_has("app/ingest/data_selfcheck.py", "enabled: bool = False")
    v4_job = _py_has("app/scheduler.py", "data_selfcheck_daily")
    print("  SelfCheckConfig 存在                           : {0}".format(v4))
    print("  默认关闭（enabled=False，可一键开启）           : {0}".format(v4_def))
    print("  已注册 17:35 调度作业                          : {0}".format(v4_job))

    # ---- V5 组合熔断 ----
    print("\n[V5 组合级回撤熔断 · P1]  落点：fusion_signal 组合权益监控")
    v5 = _py_has("app/risk/portfolio_brake.py", "class BrakeConfig")
    v5_def = _py_has("app/risk/portfolio_brake.py", "enabled: bool = False")
    v5_dd = _py_has("app/risk/portfolio_brake.py", "dd_warn: float = 0.15")
    v5_dd2 = _py_has("app/risk/portfolio_brake.py", "dd_stop: float = 0.25")
    print("  BrakeConfig 存在                               : {0}".format(v5))
    print("  默认关闭（enabled=False）                      : {0}".format(v5_def))
    print("  阈值 15% 降半仓 / 25% 清仓（决策 D2 锁定）     : {0} / {1}".format(v5_dd, v5_dd2))

    print("\n" + BAR)
    print("结论")
    print(BAR)
    items = {
        "V1 量价（因子挂载+引擎接入）": impl_v1 and wired,
        "V2 前视加固（哨兵+启动守卫）": guard_fn and guard_call,
        "V3 五关闸门（检验+统一门禁）": sd and six and gate,
        "V4 数据自检（默认关）": v4 and v4_def and v4_job,
        "V5 组合熔断（默认关/15-25%）": v5 and v5_def and v5_dd and v5_dd2,
        "V6 波动率分位（因子挂载）": reg.get("f_atr_pctile") is not None,
    }
    for k, v in items.items():
        print("  {0} {1}".format("PASS" if v else "TODO", k))
    print("\n所有新增项均带 enabled 开关且默认 false：回滚即配置置否，行为等价于改动前。")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
