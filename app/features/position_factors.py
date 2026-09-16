"""§18.4 + §18.5（v1.3）M6a：持仓/库存因子计算

纯函数，不落库，便于测试与回测接入。
因子在 as_of 之前（§18.7① 防前视）：

持仓因子（per 品种合约 as_of 日，传入 7 日 rank 切片）：
- net_long_top5: 前 5 多头总量 - 前 5 空头总量（绝对净持仓）
- cr5_long:  前 5 多头集中度（前 5 持仓 / 总持仓）
- cr5_short: 前 5 空头集中度
- net_chg_3d:  净持仓 3 日变化
- long_chg_top3_sum: 前 3 多头 3 日变化总和（聪明钱增仓信号）

库存因子（per 品种时间序列）：
- inv_change_1w:   库存周环比
- inv_change_yoy:  库存同比
- inv_zscore_60:   60 日 z-score（>1 高位）
"""
from __future__ import annotations

from datetime import date
from typing import Iterable

import numpy as np
import pandas as pd


def position_factors(rows: pd.DataFrame) -> dict:
    """由 rank 表计算持仓因子

    rows: 包含列 [rank, long_pos, short_pos, long_chg, short_chg] 的 DataFrame
          （同品种同日近 7 日窗口，长度 ≥ 1）
    """
    if rows is None or rows.empty:
        return {}
    out: dict = {}

    # 当前日
    cur = rows.sort_values("rank").iloc[0:1] if len(rows) > 0 else None
    if cur is None or cur.empty:
        return {}

    # 累计前 5 净持仓
    top5_long = rows.nsmallest(5, "rank")["long_pos"].sum() if len(rows) >= 1 else 0
    top5_short = rows.nsmallest(5, "rank")["short_pos"].sum() if len(rows) >= 1 else 0
    total_long = rows["long_pos"].sum() if len(rows) >= 1 else 0
    total_short = rows["short_pos"].sum() if len(rows) >= 1 else 0
    out["net_long_top5"] = float(top5_long - top5_short)
    out["cr5_long"] = float(top5_long / total_long) if total_long > 0 else None
    out["cr5_short"] = float(top5_short / total_short) if total_short > 0 else None

    # 净持仓 3 日变化（若有 long_chg/short_chg）
    if "long_chg" in rows.columns and len(rows) >= 1:
        out["net_chg_today"] = int(
            rows["long_chg"].sum() - rows["short_chg"].sum()
        )
        top3 = rows.nsmallest(3, "rank")
        out["long_chg_top3"] = int(top3["long_chg"].sum())
    return out


def inventory_factors(qty_series: pd.Series) -> dict:
    """库存时间序列因子

    qty_series: 索引 = report_date，值 = inventory_qty（按品种聚合后）
    """
    if qty_series is None or qty_series.empty:
        return {}
    s = qty_series.dropna().astype(float).sort_index()
    out: dict = {}
    # 周环比（5 个交易日 ≈ 7 日窗口；找最近 5 日前 vs 当前）
    if len(s) >= 2:
        out["inv_change_recent"] = float(s.iloc[-1] - s.iloc[-2])
    # 同比（252 个交易日；按日频库存只有周频点 → 退化为 4 周前对比）
    if len(s) >= 5:
        out["inv_change_4w"] = float(s.iloc[-1] - s.iloc[-5])
    # 60 日 z-score
    if len(s) >= 20:
        window = s.iloc[-60:] if len(s) >= 60 else s
        mu = float(window.mean())
        sigma = float(window.std(ddof=1)) or 1.0
        out["inv_zscore_60"] = round(float((s.iloc[-1] - mu) / sigma), 4)
    # 当前水平
    out["inv_latest"] = float(s.iloc[-1])
    out["inv_latest_date"] = s.index[-1].date().isoformat() if hasattr(s.index[-1], "date") else str(s.index[-1])
    return out


def basis_factors(df: pd.DataFrame) -> dict:
    """§18.5（v1.3.2）基差因子

    df: 列 [report_date, dom_basis_rate, dom_basis, near_basis_rate] 按时序
    """
    if df is None or df.empty:
        return {}
    s = df.dropna(subset=["dom_basis_rate"]).sort_values("report_date")
    if s.empty:
        return {}
    out: dict = {}
    out["dom_basis_rate_latest"] = float(s["dom_basis_rate"].iloc[-1])
    out["dom_basis_rate_mean_60"] = float(s["dom_basis_rate"].tail(60).mean()) if len(s) >= 1 else None
    out["dom_basis_rate_std_60"] = float(s["dom_basis_rate"].tail(60).std()) if len(s) >= 2 else None
    # 5 日基差变化（基差走强=升水扩大；走弱=升水收窄或贴水加深）
    if len(s) >= 6:
        out["basis_change_5d"] = float(s["dom_basis_rate"].iloc[-1] - s["dom_basis_rate"].iloc[-6])
    # 跨月基差差：dom_basis - near_basis（近月 vs 主力月价差信号）
    if "dom_basis" in s.columns and "near_basis" in s.columns and len(s) >= 1:
        last = s.iloc[-1]
        if pd.notna(last.get("dom_basis")) and pd.notna(last.get("near_basis")):
            out["dom_minus_near_basis"] = float(last["dom_basis"] - last["near_basis"])
    out["basis_latest_date"] = s["report_date"].iloc[-1].isoformat()
    return out
