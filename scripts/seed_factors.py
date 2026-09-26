# -*- coding: utf-8 -*-
"""因子注册表种子（因子接入 PRD §8）。

幂等 upsert 11 个 B 组横截面因子到 factor_registry。
权重说明：default_weight 符号编码方向（>0 看多、<0 看空），参与偏置乘子合成；
max_weight 为单因子权重上限，全部之和需 ≤ 1.0（T6 启动校验）。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import text

from app.core.db import get_engine

VERSION = "1.0"

# factor_id, name, description, data_sources, default_weight, max_weight, lag_days
_FACTORS = [
    ("basis_rate_z", "基差率 z", "现货-期货基差率横截面标准化（spot_basis）",
     ["spot_basis"], 0.10, 0.08, 1),
    ("member_net_z", "会员净持仓 z", "期货公司会员净持仓横截面标准化（member_position_rank）",
     ["member_position_rank"], 0.08, 0.08, 1),
    ("member_ls_z", "会员多空比 z", "会员多空持仓比横截面标准化",
     ["member_position_rank"], 0.06, 0.08, 1),
    ("vol_concentration_z", "成交量集中度 z", "前5会员成交占比(Crane)横截面标准化",
     ["member_position_rank"], -0.05, 0.08, 1),
    ("roll_yield_z", "展期收益 z", "展期收益率横截面标准化（roll_yield）",
     ["roll_yield"], 0.10, 0.08, 1),
    ("inventory_z", "库存水平 z", "期货库存水平横截面标准化（inventory）",
     ["inventory"], -0.08, 0.08, 1),
    ("warehouse_receipt_z", "仓单压力 z", "仓单数量横截面标准化（warehouse_receipt）",
     ["warehouse_receipt"], -0.06, 0.08, 1),
    ("spot_mom_z", "现货动量 z", "现货价格动量横截面标准化（spot_basis）",
     ["spot_basis"], 0.05, 0.08, 1),
    ("structure_slope_z", "期限结构斜率 z", "远近合约价差(展期结构)横截面标准化",
     ["roll_yield", "daily_bar"], 0.06, 0.08, 1),
    ("member_trend_z", "会员持仓趋势 z", "会员净持仓变化横截面标准化",
     ["member_position_rank"], 0.05, 0.08, 1),
    ("cross_rank_z", "横截面分位 z", "品种综合因子分位横截面标准化",
     ["spot_basis", "member_position_rank", "roll_yield", "inventory"], 0.04, 0.08, 1),
]


def main() -> int:
    eng = get_engine()
    stmt = text(
        """
        INSERT INTO factor_registry
            (factor_id, name, category, description, data_sources,
             min_history_days, horizon, lag_days, enabled, default_weight, max_weight, version)
        VALUES (:fid, :name, 'B', :desc, :ds, 60, 'B', :lag, true, :dw, :mw, :ver)
        ON CONFLICT (factor_id) DO UPDATE SET
            name=EXCLUDED.name, description=EXCLUDED.description,
            data_sources=EXCLUDED.data_sources, lag_days=EXCLUDED.lag_days,
            default_weight=EXCLUDED.default_weight, max_weight=EXCLUDED.max_weight
        """
    )
    with eng.begin() as conn:
        for fid, name, desc, ds, dw, mw, lag in _FACTORS:
            conn.execute(stmt, {
                "fid": fid, "name": name, "desc": desc, "ds": ds,
                "lag": lag, "dw": dw, "mw": mw, "ver": VERSION,
            })
    print(f"seeded {len(_FACTORS)} B-group factors into factor_registry")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
