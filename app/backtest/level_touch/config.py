# -*- coding: utf-8 -*-
"""level_touch 回测模块 · 全局参数。

本模块研究「价格点位触达频率」：以未复权主连的真实绝对价位为基准，
建立每个价位被区间覆盖的次数（触达图），派生支撑/阻力强度、历史最高
(ATH)、稀有层级、漂移/波动与「涨 G% 后难回买价」的递归概率，并据此生成
三类可交易假设（S1 支撑反弹 / S2 ATH 突破回踩 / S3 难回买价）做成本感知回测。

⚠ 口径铁律（来自历史教训）：
1. 只用「未复权」连续主连（barstore caliber='continuous'），绝不拿后复权做
   绝对点位研究 —— 后复权平移历史价位，使「该点位是否被触及」失真。
2. 回测执行用「次根开盘」成交（决策用 t 日收盘，成交用 t+1 日开盘），止损/
   止盈在 t+1 日 bar 内判定（保守路径），杜绝前视。
3. 换月跳空与涨跌停污染：|bar 收益率| > limit_excl 的 bar 既不计
   入触达图，也不作为信号触发日（连续主连的异常大单根几乎全是换月缺口）。
4. 成本走唯一权威 app.data.cost（含历史费率/乘数/tick/滑点），未知费率即
   报错，绝不以 0 成本静默计算。

周期说明（freq）：
- daily：日线（默认，最稳，最快）
- 60m / 15m：分钟级，信号密度更高，但需把「日级」参数缩放到「bar 级」
  （warmup / max_hold / atr_window / limit_excl 等按 bars_per_day 缩放）。
  bars_per_day 为近似值（忽略夜盘，daily=1, 60m=4, 15m=16），可在配置覆盖。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# 周期 → barstore freq 与每交易日 bar 数（近似值，忽略夜盘）
_FREQ_MAP = {
    "daily": ("daily", 1.0),
    "60m":   ("60m", 4.0),
    "15m":   ("15m", 16.0),
}


@dataclass
class LevelTouchConfig:
    # ---- 周期 ----
    freq: str = "daily"              # 'daily' | '60m' | '15m'
    allow_short: bool = False        # 是否启用空头对称信号（实证：全市场开启后净值由 +44万 转 -48万，
                                     #   因多数商品正漂移、做空趋势市吃亏；故默认关，需逐品种调参后选择性启用）

    # ---- 价格网格 ----
    # 每个价位的 bin 宽度 = max(tick_size, 日均振幅 / bins_per_range)
    bins_per_range: float = 4.0
    max_bins: int = 4000            # bin 数上限，超过则放大 bin_width 防内存爆炸

    # ---- 触达图回看 ----
    lookback_days: Optional[int] = None   # None=用全部历史；否则只用近 N 日构建触达图
    # 支撑/阻力识别：z 分数阈值 与 最少触达次数
    support_z_thr: float = 2.0
    support_min_touches: int = 8

    # ---- 信号通用 ----
    warmup_days: int = 250          # 至少需要多少历史日才允许出信号
    atr_window: int = 20            # ATR 窗口（日线语义；分钟级按 bars_per_day 缩放）
    # 触发容差：价格落入「支撑(阻力)± tol_ticks 倍 tick」内视为触及该价位带
    tol_ticks: float = 3.0
    limit_excl: float = 0.10        # |日收益| 超过此值视为换月/涨跌停，剔除

    # ---- 风控（以 ATR 为单位）----
    stop_atr_mult: float = 2.0
    target_atr_mult: float = 3.0
    max_hold_days: int = 40         # 持仓超过此天数强制平仓（防无尽漂移）

    # ---- 信号 S3「难回买价」----
    s3_drawdown_thr: float = 0.08   # 从近期高点回撤 ≥ 此值视为「坑」
    s3_recover_gain: float = 0.06   # 自坑底回升 ≥ 此值视为确认
    s3_no_return_p_thr: float = 0.6  # 拟合漂移下「不回坑底」概率 ≥ 此值才做多

    # ---- 信号 S2「ATH 突破回踩」----
    s2_ath_lookback: int = 60       # 近 N 日内创 ATH(或 ATL) 才视为有效突破
    s2_retest_tol_atr: float = 1.0  # 回踩至 ATH(ATL) 下方 ≤ 此 ATR 视为回踩

    # ---- 性能：z / 强价位列表的重算间隔（bar 数）----
    # 0 = 每 bar 重算（日线默认，行为等同旧版）；分钟级建议 = bars_per_day（每日一次）。
    z_every_bars: int = 0

    # ---- 回测范围 ----
    products: Optional[list] = None  # None=全市场；否则指定品种列表
    start: Optional[str] = None
    end: Optional[str] = None
    lots: int = 1

    # ---- 权益基线（仅用于回撤/夏普等相对指标）----
    # 点数权益从 0 起、可正负穿越，不能用其直接算回撤（峰值近 0 时除零爆炸）。
    # 统一挂到一口标准化本金上换算成人民币权益曲线，回撤 = 峰谷回撤/峰值。
    initial_capital_yuan: float = 1_000_000.0


# 默认配置实例
DEFAULT_CONFIG = LevelTouchConfig()


def resolve_freq(cfg: LevelTouchConfig):
    """把配置里的 freq 解析为 barstore 取数参数与「bar 级」派生参数。

    返回 dict：
      bs_freq            : 传给 barstore.load 的 freq
      bars_per_day      : 每交易日 bar 数（用于缩放日级→bar 级参数）
      warmup_bars       : 预热 bar 数
      max_hold_bars     : 最大持仓 bar 数
      atr_window_bars   : ATR 窗口（bar）
      s2_ath_lookback_bars : S2 回看 bar 数
      drift_window_bars : 滚动漂移/波动估计窗口（bar）
      s3_window_bars    : S3 峰谷回看窗口（bar）
      eff_limit_excl    : 按 bar 级缩放后的异常单根剔除阈值
      z_every           : z / 强价位列表重算间隔（bar）
    """
    if cfg.freq not in _FREQ_MAP:
        raise ValueError(f"未知 freq={cfg.freq!r}，可选 {list(_FREQ_MAP)}")
    bs_freq, bpd = _FREQ_MAP[cfg.freq]

    warmup_bars = int(round(cfg.warmup_days * bpd))
    max_hold_bars = int(round(cfg.max_hold_days * bpd))
    atr_window_bars = max(2, int(round(cfg.atr_window * bpd)))
    s2_ath_lookback_bars = max(2, int(round(cfg.s2_ath_lookback * bpd)))
    drift_window_bars = max(30, int(round(warmup_bars * 0.5)))
    s3_window_bars = max(30, warmup_bars)  # S3 坑结构回看窗 = 原始 250 日窗口的缩放

    # 异常单根阈值按 √bar数 缩放：日线 0.10；60m≈0.05；15m≈0.025
    eff_limit_excl = cfg.limit_excl if bpd <= 1 else cfg.limit_excl / math.sqrt(bpd)

    if cfg.z_every_bars and cfg.z_every_bars > 0:
        z_every = cfg.z_every_bars
    else:
        z_every = 1 if bpd <= 1 else max(1, int(round(bpd)))

    return dict(
        bs_freq=bs_freq, bars_per_day=bpd,
        warmup_bars=warmup_bars, max_hold_bars=max_hold_bars,
        atr_window_bars=atr_window_bars, s2_ath_lookback_bars=s2_ath_lookback_bars,
        drift_window_bars=drift_window_bars, s3_window_bars=s3_window_bars,
        eff_limit_excl=eff_limit_excl, z_every=z_every,
    )
