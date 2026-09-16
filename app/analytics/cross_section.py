"""§18.8（v1.3.2）M6c：截面相对强弱排序（研究/增强模块）

- 每日按 1 日涨跌幅对全品种排序，因子 = −rank（反转）
- 信号语义：多弱空强配对；**执行需双边流动性与做空机制**（§18.12），
  不计入 v1 主信号——仅研究跟踪与看板展示
- IC/IR 跟踪：截面 Spearman IC（rank 因子 vs 次日收益），
  按 TRAIN/TEST 划分供 §18.11② 准入判定

纯计算模块：输入 daily_bar 全品种面板，输出截面因子序列。
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
from scipy import stats


def build_cross_section(panel: pd.DataFrame) -> pd.DataFrame:
    """构造截面反转因子

    panel: 列 [trade_date, symbol, ret_close]（全品种日线面板）
    返回：panel + cs_rank（当日截面排名分位 0~1，1=当日最弱）、cs_score（多空信号：1-2*rank）
    """
    if panel is None or panel.empty:
        return pd.DataFrame()
    df = panel.dropna(subset=["ret_close"]).copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"])

    def _rank_day(g: pd.DataFrame) -> pd.DataFrame:
        # rank：0=最强（涨幅最大），1=最弱
        g = g.sort_values("ret_close", ascending=False)
        n = len(g)
        g["cs_rank"] = np.arange(n) / max(n - 1, 1)
        g["cs_score"] = 1.0 - 2.0 * g["cs_rank"]  # +1 最强 / -1 最弱
        # 反转因子：做多弱者 → signal = -cs_score
        g["cs_reversal"] = -g["cs_score"]
        return g

    return df.groupby("trade_date", group_keys=False).apply(_rank_day)


def cross_section_ic(panel: pd.DataFrame, min_names: int = 20) -> dict:
    """截面 IC/IR 跟踪：cs_reversal vs 次日 ret

    返回：{ ic_mean, ic_std, icir, t_stat, n_days, by_period }
    """
    df = build_cross_section(panel)
    if df.empty:
        return {}
    # 次日收益：shift(-1) per symbol
    df = df.sort_values(["symbol", "trade_date"])
    df["fwd_ret"] = df.groupby("symbol")["ret_close"].shift(-1)
    df = df.dropna(subset=["fwd_ret"])

    ics: list[float] = []
    dates: list[pd.Timestamp] = []
    for d, g in df.groupby("trade_date"):
        if len(g) >= min_names and g["cs_reversal"].std() > 0 and g["fwd_ret"].std() > 0:
            ic = stats.spearmanr(g["cs_reversal"], g["fwd_ret"]).statistic
            if np.isfinite(ic):
                ics.append(float(ic))
                dates.append(d)
    if len(ics) < 5:
        return {"n_days": len(ics)}
    arr = np.array(ics)
    mu = float(arr.mean())
    sd = float(arr.std(ddof=1))
    t = mu / (sd / np.sqrt(len(arr))) if sd > 0 else float("nan")
    out = {
        "ic_mean": round(mu, 4),
        "ic_std": round(sd, 4),
        "icir": round(mu / sd, 4) if sd > 0 else None,
        "t_stat": round(float(t), 2),
        "n_days": len(arr),
    }
    # 分期（2024 前后）
    cut = pd.Timestamp("2024-01-01")
    for label, mask in [("pre2024", [d < cut for d in dates]), ("post2024", [d >= cut for d in dates])]:
        seg = arr[np.array(mask)]
        if len(seg) >= 5:
            out[f"ic_{label}"] = round(float(seg.mean()), 4)
            out[f"t_{label}"] = round(
                float(seg.mean() / (seg.std(ddof=1) / np.sqrt(len(seg)))), 2
            ) if seg.std(ddof=1) > 0 else None
            out[f"n_{label}"] = int(len(seg))
    return out


def top_bottom(df_day: pd.DataFrame, k: int = 5) -> dict:
    """当日最强/最弱 top-k（看板展示用）"""
    if df_day is None or df_day.empty:
        return {"strongest": [], "weakest": []}
    g = df_day.sort_values("ret_close", ascending=False)
    strongest = [
        {"symbol": r.symbol, "ret": float(r.ret_close)}
        for r in g.head(k).itertuples()
    ]
    weakest = [
        {"symbol": r.symbol, "ret": float(r.ret_close)}
        for r in g.tail(k).iloc[::-1].itertuples()
    ]
    return {"strongest": strongest, "weakest": weakest}
