"""
特征管线（M2）
- load_canonical_series：加载权威口径主连序列（R1：csv_smooth 优先，akshare/tqsdk 兜底）
- build_features：计算 §5.1 特征集 + Hurst 状态门控
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import logger
from app.features.complexity import hurst_exponent, market_state, sample_entropy
from app.features.technical import (
    atr14,
    pct_change_n,
    ret_series,
    ret_window_stats,
    rolling_vol,
    volume_change,
)
from app.models import DailyBar, MainContinuous


@dataclass
class FeatureSnapshot:
    """一次预测的特征快照（含溯源信息）"""

    symbol: str
    source: str                       # csv_smooth / akshare_tqsdk
    rets: np.ndarray                  # 权威口径收益率序列 %
    dates: pd.Index = None            # rets 对应日期索引（M3 传导特征对齐用）
    close: pd.Series = None
    high: pd.Series = None
    low: pd.Series = None
    volume: pd.Series = None
    last_date: date | None = None
    features: dict = field(default_factory=dict)
    hurst: float | None = None
    sample_entropy: float | None = None
    state: str = "neutral"


def load_canonical_series(session: Session, symbol: str) -> tuple[pd.DataFrame, str]:
    """加载权威口径序列（R1 + 滞后回退）

    1) csv_smooth：main_continuous.adj_*（用户后复权主连，⑪ 权威口径）
       —— 但 CSV 为静态快照，若明显滞后于 daily_bar（> 3 天），自动回退到 2)
          并告警（M2.1 待办：平滑主连自动延伸，见实现问题清单）
    2) akshare_tqsdk：daily_bar（主连品种的 akshare/tqsdk 连续价，未平滑）
    返回 (DataFrame[trade_date index], source)
    """
    settings = get_settings()
    product = symbol[:-3] if symbol.upper().endswith("888") else symbol

    def _daily_df() -> pd.DataFrame:
        rows = session.execute(
            select(
                DailyBar.trade_date,
                DailyBar.close,
                DailyBar.high,
                DailyBar.low,
                DailyBar.volume,
            )
            .where(DailyBar.symbol == symbol)
            .order_by(DailyBar.trade_date)
        ).all()
        return pd.DataFrame(
            [r for r in rows if r[1] is not None],
            columns=["trade_date", "close", "high", "low", "volume"],
        ).set_index("trade_date")

    # 1. CSV 平滑主连
    rows = session.execute(
        select(
            MainContinuous.trade_date,
            MainContinuous.adj_close,
            MainContinuous.adj_high,
            MainContinuous.adj_low,
            MainContinuous.adj_volume,
        )
        .where(MainContinuous.product == product)
        .order_by(MainContinuous.trade_date)
    ).all()
    if rows:
        df_csv = pd.DataFrame(
            [r for r in rows if r[1] is not None],
            columns=["trade_date", "close", "high", "low", "volume"],
        ).set_index("trade_date")
        if len(df_csv) >= settings.yaml.predict.min_bars:
            last_daily = session.execute(
                select(DailyBar.trade_date)
                .where(DailyBar.symbol == symbol)
                .order_by(DailyBar.trade_date.desc())
                .limit(1)
            ).scalar()
            if last_daily is None or (last_daily - df_csv.index[-1]).days <= 3:
                return df_csv, "csv_smooth"
            lag = (last_daily - df_csv.index[-1]).days
            logger.warning(
                f"[features] {symbol} csv_smooth 滞后 {lag} 天"
                f"（csv 止于 {df_csv.index[-1]}，daily_bar 止于 {last_daily}），"
                f"回退 daily_bar 口径；M2.1 待实现平滑主连自动延伸"
            )

    # 2. daily_bar 兜底
    df = _daily_df()
    return df, "akshare_tqsdk"


def features_from_series(symbol: str, df: pd.DataFrame, source: str) -> FeatureSnapshot:
    """由（截至评估日的）序列计算特征快照（M4：回测历史切片复用同一逻辑）"""
    cfg = get_settings().yaml.predict
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)

    rets = ret_series(close).dropna()
    ret_arr = rets.to_numpy()

    snap = FeatureSnapshot(
        symbol=symbol,
        source=source,
        rets=ret_arr[-cfg.history_bars:],
        dates=rets.index[-cfg.history_bars:],
        close=close,
        high=high,
        low=low,
        volume=volume,
        last_date=close.index[-1],
    )

    feats: dict = {"ret1": float(rets.iloc[-1])}
    for n in cfg.windows.ret:
        v = pct_change_n(close, n)
        if v is not None:
            feats[f"ret{n}"] = v
    for n in cfg.windows.vol:
        v = rolling_vol(rets, n)
        if v is not None:
            feats[f"vol{n}"] = v
    a = atr14(high, low, close, 14)
    if a is not None:
        feats["atr14_pct"] = a
    vc = volume_change(volume, 5)
    if vc is not None:
        feats["volume_change_5"] = vc
    feats.update(ret_window_stats(ret_arr, cfg.windows.ret[-1]))
    snap.features = feats

    # Hurst / 样本熵 / 状态门控（§5.3）
    h_window = ret_arr[-cfg.hurst.window :]
    snap.hurst = hurst_exponent(h_window)
    snap.sample_entropy = sample_entropy(h_window)
    snap.state = market_state(
        snap.hurst, cfg.hurst.trend_threshold, cfg.hurst.mean_revert_threshold
    )
    return snap


def build_features(session: Session, symbol: str, as_of: date | None = None) -> FeatureSnapshot:
    """计算 §5.1 特征集 + Hurst 状态门控（as_of 可指定历史截止日，防前视）"""
    cfg = get_settings().yaml.predict
    df, source = load_canonical_series(session, symbol)
    if as_of is not None:
        df = df[df.index <= as_of]
    if len(df) < cfg.min_bars:
        raise ValueError(
            f"{symbol} 有效数据仅 {len(df)} 根 < min_bars={cfg.min_bars}（source={source}）"
        )
    snap = features_from_series(symbol, df, source)
    logger.info(
        f"[features] {symbol} source={source} bars={len(df)} last={snap.last_date} "
        f"state={snap.state} hurst={snap.hurst} sampen={snap.sample_entropy}"
    )
    return snap
