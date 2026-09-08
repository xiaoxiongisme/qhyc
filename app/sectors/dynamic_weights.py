"""
§16.2 第 2 层：数据驱动动态传导权重（M3 排期）

对每条先验链 (src → dst)，用近 120 日收益率序列计算：
- 相关系数：60/120 日 Pearson 相关绝对值（方向由先验定）
- Granger 因果：滞后阶搜索 {1,2,5}，min p-value → strength = 1 - p
- 传递熵（TE）：中位数二值化离散 TE，lag ∈ {1,2,5} 搜索最优，按条件熵归一
- VAR 脉冲响应：dst 对 src 正交冲击 5 期响应峰值归一

融合（§16.2）：w = 0.5×先验 + 0.5×数据（数据 = corr/granger/te 可用项均值）；
样本不足（重叠日 < 60）回落纯先验。
落库 transmission_weights：method=corr/granger/te/var/blend 各一行；
每周重算（与 LSTM 周训对齐，§16.7 ⑤）。
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.models import DailyBar, TransmissionWeight

LOOKBACK = 120
MIN_SAMPLES = 60
GRANGER_LAGS = (1, 2, 5)
TE_LAGS = (1, 2, 5)


def _ret_frame(session: Session, src: str, dst: str, lookback: int = LOOKBACK) -> pd.DataFrame | None:
    """两品种 ret_close 内连接对齐（含滞后补偿：src 结束日可比 dst 晚 5 日）"""
    def load(p):
        rows = session.execute(
            select(DailyBar.trade_date, DailyBar.ret_close)
            .where(DailyBar.symbol == f"{p}888", DailyBar.ret_close.is_not(None))
            .order_by(DailyBar.trade_date.desc())
            .limit(lookback + 10)
        ).all()
        return pd.Series({r[0]: float(r[1]) for r in rows[::-1]})

    s_src, s_dst = load(src), load(dst)
    if s_src.empty or s_dst.empty:
        return None
    df = pd.DataFrame({"src": s_src, "dst": s_dst}).dropna()
    if len(df) < MIN_SAMPLES:
        return None
    return df.tail(lookback)


# ---------------- 各方法 ----------------

def corr_strength(df: pd.DataFrame) -> float | None:
    """60/120 日相关绝对值均值 → [0,1]"""
    n = len(df)
    if n < MIN_SAMPLES:
        return None
    c120 = float(df["src"].corr(df["dst"]))
    c60 = float(df["src"].tail(60).corr(df["dst"].tail(60))) if n >= 60 else c120
    if np.isnan(c120) or np.isnan(c60):
        return None
    return float(min(1.0, (abs(c120) + abs(c60)) / 2))


def granger_strength(df: pd.DataFrame) -> float | None:
    """Granger 因果（src→dst），滞后阶 {1,2,5} 搜索 min p → [0,1]"""
    from statsmodels.tsa.stattools import grangercausalitytests
    import warnings

    best_p = None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            # 检验 src 是否 Granger-cause dst：因变量 dst 在前
            res = grangercausalitytests(df[["dst", "src"]], maxlag=max(GRANGER_LAGS), verbose=False)
            for lag in GRANGER_LAGS:
                p = float(res[lag][0]["ssr_ftest"][1])
                best_p = p if best_p is None else min(best_p, p)
        except Exception as e:
            logger.debug(f"[dynw] granger 失败: {e}")
            return None
    if best_p is None or np.isnan(best_p):
        return None
    return float(min(1.0, max(0.0, 1 - best_p)))


def _binarize(x: pd.Series) -> np.ndarray:
    med = x.median()
    return (x > med).astype(int).to_numpy()


def te_strength(df: pd.DataFrame) -> tuple[float | None, int | None]:
    """传递熵（离散，中位数二值化）：TE(X_{t-L}; Y_t | Y_{t-1})，lag 搜索 → bits，归一 [0,1]"""
    from collections import Counter

    xb = _binarize(df["src"])
    yb = _binarize(df["dst"])
    n = len(yb)
    best_te, best_lag = None, None
    for L in TE_LAGS:
        if n - L - 1 < 30:
            continue
        y_t = yb[L:]
        y_prev = yb[L - 1 : n - 1]
        x_lag = xb[: n - L]
        N = len(y_t)
        n_abc = Counter(zip(x_lag, y_prev, y_t))
        n_bc = Counter(zip(y_prev, y_t))
        n_b = Counter(y_prev)
        te = 0.0
        for (a, b, c), nabc in n_abc.items():
            p_abc = nabc / N
            p_c_ab = nabc / max(n_bc[(b, c)], 1)
            p_c_b = (
                sum(v for (bb, cc), v in n_bc.items() if bb == b and cc == c)
                / max(n_b[b], 1)
            )
            if p_c_ab > 0 and p_c_b > 0:
                te += p_abc * np.log2(p_c_ab / p_c_b)
        if best_te is None or te > best_te:
            best_te, best_lag = te, L
    if best_te is None or best_te <= 0:
        return None, None
    # 二值信源条件熵上界 1 bit，归一到 [0,1]
    return float(min(1.0, best_te)), best_lag


def var_strength(df: pd.DataFrame) -> float | None:
    """VAR 脉冲响应：dst 对 src 正交冲击 5 期峰值响应归一"""
    from statsmodels.tsa.api import VAR
    import warnings

    if len(df) < MIN_SAMPLES:
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            model = VAR(df[["src", "dst"]].to_numpy())
            res = model.fit(maxlags=2)
            irf = res.irf(5)
            # orth_irf[sim, period, response_var]：src=index0, dst=index1
            resp = np.asarray(irf.orth_irfs)[:, :, 1]  # dst 对 src 冲击
            peak = float(np.abs(resp).max())
        except Exception as e:
            logger.debug(f"[dynw] var 失败: {e}")
            return None
    # 归一：响应幅度相对 dst 自身日波动
    vol = float(df["dst"].std(ddof=1)) or 1.0
    return float(min(1.0, peak / (2 * vol)))


# ---------------- 主流程 ----------------

def calc_dynamic_weights(session: Session, lookback: int = LOOKBACK) -> dict:
    """对全部先验链计算动态权重并落库；返回统计"""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.sectors.builder import load_transmission_config

    chains = (load_transmission_config().get("chains") or [])
    results = {"chains": len(chains), "calculated": 0, "fallback_prior": 0, "rows": 0}

    for c in chains:
        src, dst = str(c["src"]).upper(), str(c["dst"]).upper()
        prior = float(c.get("strength", 0.5))
        lag_days = int(c.get("lag_days", 1))
        cost_ratio = c.get("cost_ratio")

        df = _ret_frame(session, src, dst, lookback)
        method_rows: list[dict] = []

        if df is None:
            results["fallback_prior"] += 1
            method_rows.append(_row(src, dst, "blend", lag_days, prior, cost_ratio))
        else:
            data_strengths: list[float] = []
            c_ = corr_strength(df)
            g_ = granger_strength(df)
            t_, t_lag = te_strength(df)
            v_ = var_strength(df)
            for method, s, lag in (
                ("corr", c_, None), ("granger", g_, None), ("te", t_, t_lag), ("var", v_, None)
            ):
                if s is not None:
                    data_strengths.append(s)
                    method_rows.append(_row(src, dst, method, lag if lag else lag_days, s, cost_ratio))
            if data_strengths:
                data_avg = float(np.mean(data_strengths))
                blend = 0.5 * prior + 0.5 * data_avg
                method_rows.append(_row(src, dst, "blend", lag_days, blend, cost_ratio))
                results["calculated"] += 1
            else:
                results["fallback_prior"] += 1
                method_rows.append(_row(src, dst, "blend", lag_days, prior, cost_ratio))

        for r in method_rows:
            stmt = pg_insert(TransmissionWeight).values(**r)
            stmt = stmt.on_conflict_do_update(
                index_elements=["src_product", "dst_product", "method", "lag_days"],
                set_={
                    "direction": stmt.excluded.direction,
                    "weight": stmt.excluded.weight,
                    "cost_ratio": stmt.excluded.cost_ratio,
                    "updated_at": text("NOW()"),
                },
            )
            session.execute(stmt)
            results["rows"] += 1

    session.commit()
    logger.info(f"[dynw] 动态权重完成: {results}")
    return results


def _row(src, dst, method, lag_days, weight, cost_ratio) -> dict:
    return {
        "src_product": src,
        "dst_product": dst,
        "method": method,
        "lag_days": int(lag_days),
        "direction": "positive",
        "weight": round(float(min(1.0, max(0.0, weight))), 4),
        "cost_ratio": float(cost_ratio) if cost_ratio is not None else None,
    }