# -*- coding: utf-8 -*-
"""level_touch 回测模块。

研究「价格点位触达频率」对期货历史数据打标签、做自我分析与演进，并据其
生成三类可交易假设做成本感知回测。详见 analyze.py / README.md。

子模块：
  config   —— 全局参数（口径铁律集中在此）
  engine   —— 纯数学引擎（触达图/局部时间/z分数/漂移波动/递归概率），可单测
  signals  —— S1 支撑反弹 / S2 ATH 突破回踩 / S3 难回买价（含空头对称）
  backtest —— 次根开盘 + bar内止损止盈 + app.data.cost 成本，无前视；
             支持 daily/60m/15m 多周期与双向持仓
  analyze  —— 全市场编排与产物
  sweep    —— 参数扫描（敏感度 / 最优配置搜索）
"""
from .config import DEFAULT_CONFIG, LevelTouchConfig, resolve_freq
from .engine import (add_box, add_range, auto_bin_width, fit_drift_vol,
                     gain_for_threshold, make_grid, recurrence_no_return,
                     singleton_levels, strong_levels, support_zones,
                     topology_summary, z_score, _rolling_mean_std,
                     cumulative_extreme)
from .signals import generate_signal

__all__ = ["LevelTouchConfig", "DEFAULT_CONFIG", "resolve_freq", "engine",
           "signals", "backtest", "analyze", "sweep"]
