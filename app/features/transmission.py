"""
§16.3 传导特征 v1（M2 排期：静态先验层）

全部使用 as_of 之前的数据（防前视，§16.7 ①）：
- sector_ret_1d/5d/20d：所属大类指数动量（sector_index 表）
- sector_excess：品种当日涨跌 − 大类当日涨跌（板块内强弱/背离）
- upstream_ret_lag1/lag5：top-k（默认 3）上游品种的滞后涨跌幅（按先验强度加权）
- cost_gap：Σ(上游 lag1 ret × 成本占比) − 品种实际 ret（背离=信号）
- sector_corr_regime：板块内 60 日平均两两相关
- te_topk：M2 用先验 top-3 上游 lag1 均值（M3 换传递熵）

输出：DataFrame[trade_date] 逐日序列（供 ML 特征集）+ 最新行快照（供单模型诊断）
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import logger
from app.models import DailyBar, SectorIndex, SectorMap, TransmissionWeight

FEATURE_COLS = [
    "sector_ret_1d",
    "sector_ret_5d",
    "sector_ret_20d",
    "sector_excess",
    "upstream_ret_lag1",
    "upstream_ret_lag5",
    "cost_gap",
    "sector_corr_regime",
    "te_topk",
    "sector_pred_prob",
]


def _product_ret_series(session: Session, product: str, end: date) -> pd.Series:
    """品种日收益序列（截止 end，含 end；§16.7 ① 滞后口径）"""
    rows = session.execute(
        select(DailyBar.trade_date, DailyBar.ret_close)
        .where(
            DailyBar.symbol == f"{product}888",
            DailyBar.ret_close.is_not(None),
            DailyBar.trade_date <= end,
        )
        .order_by(DailyBar.trade_date)
    ).all()
    return pd.Series({r[0]: float(r[1]) for r in rows})


def build_transmission_frame(session: Session, product: str, end: date, lookback: int = 260) -> pd.DataFrame:
    """构造逐日传导特征序列（index=trade_date，最近 lookback 根）"""
    cfg = get_settings().yaml.predict
    topk = get_transmission_cfg().get("transmission", {}).get("topk_upstream", 3)
    corr_window = get_transmission_cfg().get("transmission", {}).get("corr_window", 60)

    tproduct = product[:-3] if product.upper().endswith("888") else product
    sector = session.execute(
        select(SectorMap.sector).where(SectorMap.product == tproduct.upper())
    ).scalar()

    # 1. 大类指数
    idx = pd.DataFrame()
    if sector:
        rows = session.execute(
            select(SectorIndex)
            .where(
                SectorIndex.sector == sector,
                SectorIndex.trade_date <= end,
            )
            .order_by(SectorIndex.trade_date)
        ).scalars().all()
        if rows:
            idx = pd.DataFrame(
                [
                    {
                        "trade_date": r.trade_date,
                        "sector_ret_1d": float(r.ret_1d) if r.ret_1d is not None else np.nan,
                        "sector_ret_5d": float(r.ret_5d) if r.ret_5d is not None else np.nan,
                        "sector_ret_20d": float(r.ret_20d) if r.ret_20d is not None else np.nan,
                    }
                    for r in rows
                ]
            ).set_index("trade_date")

    # 2. 品种自身收益
    own = _product_ret_series(session, tproduct, end)

    # 3. 上游（§16.2 v2：优先真实传递熵 method=te → blend → prior 回落）
    method_priority = ("te", "blend", "prior")
    best: dict[str, float] = {}
    cost_ratio: dict[str, float] = {}
    used_method = None
    for m in method_priority:
        upstream = session.execute(
            select(TransmissionWeight)
            .where(
                TransmissionWeight.dst_product == tproduct.upper(),
                TransmissionWeight.method == m,
            )
        ).scalars().all()
        if upstream:
            used_method = m
            for u in upstream:
                s = float(u.weight)
                if s > best.get(u.src_product, -1):
                    best[u.src_product] = s
                    if u.cost_ratio is not None:
                        cost_ratio[u.src_product] = float(u.cost_ratio)
            if best:
                break
    top_srcs = sorted(best, key=lambda k: best[k], reverse=True)[:topk]

    # 上游品种收益序列
    src_rets: dict[str, pd.Series] = {}
    for src in top_srcs:
        src_rets[src] = _product_ret_series(session, src, end)

    # 4. 板块内 60 日平均两两相关（rolling 窗口逐日）
    sector_products = [
        r[0]
        for r in session.execute(
            select(SectorMap.product).where(SectorMap.sector == sector)
        ).all()
    ] if sector else []
    corr_series = pd.Series(dtype=float)
    if len(sector_products) >= 2:
        ret_m = pd.DataFrame({p: _product_ret_series(session, p, end) for p in sector_products})
        ret_m = ret_m.sort_index()
        # 两两相关滚动：60 日窗口截面相关上三角均值
        pair_corr = ret_m.rolling(corr_window).corr()
        vals = {}
        for d in ret_m.index[corr_window:]:
            sub = pair_corr.loc[d]
            mat = sub.unstack() if isinstance(sub.index, pd.MultiIndex) else sub
            m = mat.to_numpy(dtype=float)
            n = m.shape[0]
            tri = m[np.triu_indices(n, k=1)]
            tri = tri[~np.isnan(tri)]
            if tri.size:
                vals[d] = float(tri.mean())
        corr_series = pd.Series(vals)

    # §16.2 第 3 层：板块指数最新预测（sector_pred_prob，层级预测先行落库）
    sector_pred_prob = np.nan
    if sector:
        try:
            from app.engine.service import latest_sector_prediction

            sp = latest_sector_prediction(session, sector)
            if sp and sp.get("direction_prob") is not None:
                p = float(sp["direction_prob"])
                sector_pred_prob = p if sp["direction"] == "up" else 1 - p
        except Exception as e:
            logger.debug(f"[transmission] sector_pred_prob 读取失败: {e}")

    # 5. 逐日合成特征表
    dates = own.index[-lookback:]
    rows = []
    for d in dates:
        f = {
            "sector_ret_1d": np.nan,
            "sector_ret_5d": np.nan,
            "sector_ret_20d": np.nan,
        }
        if d in idx.index:
            for c in ("sector_ret_1d", "sector_ret_5d", "sector_ret_20d"):
                v = idx.at[d, c]
                f[c] = v if pd.notna(v) else np.nan
        own_ret = own.get(d, np.nan)
        f["sector_excess"] = (
            own_ret - f["sector_ret_1d"] if pd.notna(own_ret) and pd.notna(f["sector_ret_1d"]) else np.nan
        )

        # 上游滞后收益（加权）与 lag5
        w_sum1 = w_sum5 = 0.0
        acc1 = acc5 = 0.0
        loc = own.index.get_loc(d)
        d_lag1 = own.index[loc - 1] if loc >= 1 else None
        d_lag5 = own.index[loc - 5] if loc >= 5 else None
        top3_vals = []
        for src in top_srcs:
            sr = src_rets.get(src)
            if sr is None:
                continue
            w = best.get(src, 0.5)
            if d_lag1 is not None and d_lag1 in sr.index:
                acc1 += w * sr.loc[d_lag1]
                w_sum1 += w
                top3_vals.append(sr.loc[d_lag1])
            if d_lag5 is not None and d_lag5 in sr.index:
                acc5 += w * sr.loc[d_lag5]
                w_sum5 += w
        f["upstream_ret_lag1"] = acc1 / w_sum1 if w_sum1 > 0 else np.nan
        f["upstream_ret_lag5"] = acc5 / w_sum5 if w_sum5 > 0 else np.nan
        f["te_topk"] = float(np.mean(top3_vals)) if top3_vals else np.nan

        # cost_gap = Σ(src lag1 ret × cost_ratio) − 品种当日 ret
        cost_terms = [
            sr.loc[d_lag1] * cost_ratio.get(src)
            for src in top_srcs
            if src in cost_ratio and d_lag1 is not None and d_lag1 in src_rets.get(src, pd.Series()).index
        ]
        cost_terms = [t for t in cost_terms if pd.notna(t)]
        f["cost_gap"] = (sum(cost_terms) - own_ret) if (cost_terms and pd.notna(own_ret)) else np.nan

        f["sector_corr_regime"] = corr_series.get(d, np.nan)

        # §16.2 第 3 层：层级预测结果（sector_pred_prob，最新已落库的板块指数预测）
        f["sector_pred_prob"] = sector_pred_prob

        rows.append({"trade_date": d, **f})

    df = pd.DataFrame(rows).set_index("trade_date")
    return df


def transmission_snapshot(session: Session, product: str, end: date) -> dict:
    """最新一行的传导特征快照（供预测响应 drivers / 看板传导卡片 M5）"""
    df = build_transmission_frame(session, product, end, lookback=1)
    if df.empty:
        return {}
    row = df.iloc[-1].to_dict()
    return {
        k: (round(float(v), 4) if v is not None and np.isfinite(v) else None)
        for k, v in row.items()
    }


def get_transmission_cfg() -> dict:
    """读取 transmission_config.yaml（非重缓存）"""
    from app.sectors.builder import load_transmission_config

    return load_transmission_config()
