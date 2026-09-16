"""M6a 端到端验证：FG 持仓 + 库存 + 因子计算"""
import sys
sys.path.insert(0, "/app")
import warnings; warnings.filterwarnings("ignore")

from sqlalchemy import text
import pandas as pd

from app.core.db import session_scope
from app.features.position_factors import position_factors, inventory_factors

with session_scope() as s:
    # 1. FG 前 5 多头
    rows = s.execute(text(
        "SELECT exchange, symbol, member, rank, long_pos, short_pos, long_chg, short_chg "
        "FROM member_position_rank WHERE symbol LIKE 'FG%' AND rank <= 5 "
        "ORDER BY symbol, rank"
    )).all()
    print(f"FG 持仓前 5（{len(rows)} 行）:")
    for r in rows[:5]:
        print(f"  {r[0]} {r[1]} rank={r[3]} {r[2][:14]:<14s} L={r[4]:>8d} S={r[5]:>8d} dL={r[6] or 0:+d} dS={r[7] or 0:+d}")
    # 因子
    if rows:
        df = pd.DataFrame([{
            "rank": r[3], "long_pos": r[4], "short_pos": r[5],
            "long_chg": r[6] or 0, "short_chg": r[7] or 0
        } for r in rows])
        print(f"\nFG 持仓因子: {position_factors(df)}")

    # 2. 库存
    inv = s.execute(text(
        "SELECT report_date, inventory_qty FROM inventory WHERE symbol='FG' ORDER BY report_date"
    )).all()
    if inv:
        ts = pd.Series([r[1] for r in inv], index=pd.to_datetime([r[0] for r in inv]))
        print(f"\nFG 库存 ({len(inv)} 点, 最早 {inv[0][0]} ~ 最近 {inv[-1][0]})")
        print(f"  因子: {inventory_factors(ts)}")

    # 3. 总规模
    n_pos = s.execute(text("SELECT count(*), count(DISTINCT trade_date) FROM member_position_rank")).first()
    n_inv = s.execute(text("SELECT count(*), count(DISTINCT report_date), count(DISTINCT symbol) FROM inventory")).first()
    print(f"\n=== 全库规模 ===")
    print(f"member_position_rank: {n_pos[0]} 行, {n_pos[1]} 个交易日")
    print(f"inventory: {n_inv[0]} 行, {n_inv[1]} 个报告日, {n_inv[2]} 品种")
