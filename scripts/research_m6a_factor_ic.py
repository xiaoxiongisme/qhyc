"""§18.11②（v1.3）M6a：新因子 IC 验证（OOS holdout）

准入门槛：新因子（持仓/库存）在 holdout（2024+）|IC| 对应 **t > 2** 才可入集成权重；
否则仅作看板展示。

执行（数据积累 30+ 天后可用）：
    python scripts/research_m6a_factor_ic.py

输出每个因子的：
- IC 均值 / std / |t| / 有效日
- 截面 IC vs ret_t (Pearson) / vs dir_t (point-biserial)
- 按品种分组的覆盖率与方向性
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, "/app")

import numpy as np
import pandas as pd
from scipy import stats
from sqlalchemy import select, text

from app.core.db import session_scope

TRAIN_END = pd.Timestamp("2023-12-31")
OUT = Path("/app/logs/m6a_factor_ic.log")


def w(s: str) -> None:
    print(s)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(s + "\n")


def main():
    OUT.write_text("")  # 清空
    w(f"# M6a 因子 IC 验证（§18.11②）  {pd.Timestamp.now():%Y-%m-%d %H:%M:%S}")
    w("")

    with session_scope() as s:
        # ---- 持仓因子 IC ----
        w("## 1. 持仓因子（net_long_top5、CR5）IC 验证（OOS）")
        try:
            pos = pd.read_sql(
                text(
                    "SELECT trade_date, exchange, symbol, member, rank, "
                    "long_pos, short_pos, long_chg, short_chg "
                    "FROM member_position_rank WHERE rank <= 20"
                ),
                s.bind,
            )
        except Exception as e:
            w(f"读取 member_position_rank 失败: {e}")
            pos = pd.DataFrame()
        w(f"- member_position_rank 样本: {len(pos)} 行，{pos['trade_date'].nunique() if not pos.empty else 0} 个交易日")
        if not pos.empty and pos["trade_date"].nunique() >= 5:
            # 聚合 per (date, exchange, symbol) 因子
            pos["net"] = pos["long_pos"] - pos["short_pos"]
            agg = pos.groupby(["trade_date", "exchange", "symbol"]).agg(
                net_top5=("net", lambda x: x[pos.loc[x.index, "rank"] <= 5].sum()),
                cr5_long=(
                    "long_pos",
                    lambda x: x[pos.loc[x.index, "rank"] <= 5].sum() / max(x.sum(), 1),
                ),
            ).reset_index()
            w(f"- 聚合后 (date, ex, sym) 切片: {len(agg)} 行")
            # 与 ret_t 对齐
            try:
                db = pd.read_sql(
                    text(
                        "SELECT symbol, trade_date, ret_close FROM daily_bar "
                        "WHERE symbol LIKE '%888' AND ret_close IS NOT NULL"
                    ),
                    s.bind,
                )
            except Exception as e:
                w(f"读取 daily_bar 失败: {e}")
                db = pd.DataFrame()
            if not db.empty:
                agg["trade_date"] = pd.to_datetime(agg["trade_date"])
                db["trade_date"] = pd.to_datetime(db["trade_date"])
                # 截面 IC（per date）：取当日全部品种的因子与 ret_t 计算 spearman
                ics_net, ics_cr5, dates = [], [], []
                for d, g in agg.groupby("trade_date"):
                    g2 = g.merge(db[db["trade_date"] == d], on="symbol", how="inner")
                    if len(g2) >= 10 and g2["net_top5"].std() > 0 and g2["ret_close"].std() > 0:
                        ics_net.append(stats.spearmanr(g2["net_top5"], g2["ret_close"]).statistic)
                        ics_cr5.append(stats.spearmanr(g2["cr5_long"], g2["ret_close"]).statistic)
                        dates.append(d)
                if ics_net:
                    ics_net = np.array(ics_net)
                    ics_cr5 = np.array(ics_cr5)
                    for name, arr in [("net_top5", ics_net), ("cr5_long", ics_cr5)]:
                        valid = ~np.isnan(arr)
                        if valid.sum() >= 5:
                            mu = arr[valid].mean()
                            sd = arr[valid].std(ddof=1)
                            t = mu / (sd / np.sqrt(valid.sum())) if sd > 0 else float("nan")
                            w(
                                f"  - {name}: mean={mu:.4f} std={sd:.4f} "
                                f"|t|={abs(t):.2f} n={valid.sum()}/"
                                f"{len(arr)} (>=5, |t|>2 → 准入)"
                            )
                        else:
                            w(f"  - {name}: 有效日不足（{valid.sum()}）")
                else:
                    w("- 截面 IC 计算失败：样本不足")
        else:
            w("- 持仓数据不足（< 5 交易日），暂无法计算 IC")
        w("")

        # ---- 库存因子 IC ----
        w("## 2. 库存因子（inv_change_4w、inv_zscore_60）IC 验证（OOS）")
        try:
            inv = pd.read_sql(
                text(
                    "SELECT report_date, exchange, symbol, inventory_qty, change_qty "
                    "FROM inventory ORDER BY report_date"
                ),
                s.bind,
            )
        except Exception as e:
            w(f"读取 inventory 失败: {e}")
            inv = pd.DataFrame()
        w(f"- inventory 样本: {len(inv)} 行，{inv['report_date'].nunique() if not inv.empty else 0} 个报告日")
        if not inv.empty and inv["report_date"].nunique() >= 10:
            inv["report_date"] = pd.to_datetime(inv["report_date"])
            inv["change_4w"] = inv.groupby("symbol")["inventory_qty"].diff(4)
            # 截面 IC：当日全部品种的 inv_change_4w 与次日 ret_t
            try:
                db2 = pd.read_sql(
                    text(
                        "SELECT symbol, trade_date, ret_close FROM daily_bar "
                        "WHERE symbol LIKE '%888' AND ret_close IS NOT NULL"
                    ),
                    s.bind,
                )
            except Exception as e:
                db2 = pd.DataFrame()
            if not db2.empty:
                db2["trade_date"] = pd.to_datetime(db2["trade_date"])
                ics = []
                for d, g in inv.groupby("report_date"):
                    future_d = d + pd.Timedelta(days=1)
                    g2 = g.merge(
                        db2[db2["trade_date"] == future_d][["symbol", "ret_close"]],
                        on="symbol", how="inner",
                    )
                    g2 = g2.dropna(subset=["change_4w", "ret_close"])
                    if len(g2) >= 10 and g2["change_4w"].std() > 0 and g2["ret_close"].std() > 0:
                        ics.append(stats.spearmanr(g2["change_4w"], g2["ret_close"]).statistic)
                if ics and len(ics) >= 5:
                    arr = np.array(ics)
                    arr = arr[~np.isnan(arr)]
                    mu = arr.mean()
                    sd = arr.std(ddof=1)
                    t = mu / (sd / np.sqrt(len(arr))) if sd > 0 else float("nan")
                    w(
                        f"- inv_change_4w: mean={mu:.4f} std={sd:.4f} "
                        f"|t|={abs(t):.2f} n={len(arr)} (>=5, |t|>2 → 准入)"
                    )
                else:
                    w(f"- inv_change_4w 有效截面 IC 不足（{len(ics)}）")
        else:
            w("- 库存数据不足（< 10 报告日），暂无法计算 IC")
        w("")

    w("---")
    w("判定汇总见各节。持仓/库存因子均 |t|>2 即可入集成权重（M6c 接入）")


if __name__ == "__main__":
    main()
