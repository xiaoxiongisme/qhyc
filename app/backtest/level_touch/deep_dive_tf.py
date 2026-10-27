# -*- coding: utf-8 -*-
"""level_touch · 强边缘品种分钟级深挖（验证"更高频→更高信号密度"假设）。

对样本内强边缘品种在 daily / 60m / 15m 三周期跑回测，对比：
  - n_trades（信号密度，应随频率升高而升高）
  - PF / 净利 / 胜率（边缘是否随频率提升或更稳定）

品种：SR(白糖)/AU(黄金)/AG(白银)/SC(原油)/LC(碳酸锂)/SS(不锈钢)/SA(纯碱)/NI(镍)/RU(橡胶)
（均为白名单内且样本内 PF 较高者）

在容器内运行：
  docker exec qhyc-api PYTHONPATH=/app python -m app.backtest.level_touch.deep_dive_tf
"""
from __future__ import annotations

import os
from dataclasses import replace
from typing import Optional

import pandas as pd

from .config import DEFAULT_CONFIG, LevelTouchConfig
from .variety_config import WHITELIST_SHORT

# 强边缘品种（白名单内样本内 PF 较高）
VARIETIES = ["SR", "AU", "AG", "SC", "LC", "SS", "SA", "NI", "RU"]
FREQS = ["daily", "60m", "15m"]

# 深挖用的"较好默认"配置（白名单品种开空头 + 中等风控）
_BASE_OVERRIDES = dict(
    allow_short=True,
    support_z_thr=2.0,
    support_min_touches=8,
    stop_atr_mult=2.0,
    target_atr_mult=3.0,
    s3_no_return_p_thr=0.6,
    z_every_bars=0,   # 日线每次重算；分钟级在 resolve_freq 内自动按 bpd 设
)


def deep_dive(varieties: Optional[list] = None,
              out_dir: str = "/app/qh/level_touch_deep") -> pd.DataFrame:
    varieties = varieties or VARIETIES
    from app.data import barstore
    from app.data.barstore import variety_spec
    from datetime import date
    from .backtest import run_backtest
    from .config import resolve_freq

    rows = []
    for v in varieties:
        for freq in FREQS:
            cfg = replace(DEFAULT_CONFIG, freq=freq, **_BASE_OVERRIDES)
            fr = resolve_freq(cfg)
            bars = barstore.load(v, fr["bs_freq"], "continuous",
                                 cfg.start, cfg.end)
            if bars.empty or len(bars) < fr["warmup_bars"] + 5:
                print(f"  {v}/{freq}: 数据不足跳过")
                continue
            try:
                spec = variety_spec(v, as_of=date.today())
                tick = float(spec["tick_size"])
                mult = float(spec["multiplier"])
            except Exception as e:
                print(f"  {v}/{freq}: 规格缺失 {e}")
                continue
            try:
                r = run_backtest(bars, v, cfg, tick, mult)
            except Exception as e:
                print(f"  {v}/{freq}: 回测异常 {e}")
                r = {"n_trades": 0, "pf": 0.0, "total_net_yuan": 0.0,
                     "win_rate": 0.0, "max_dd": 0.0, "sharpe": 0.0,
                     "avg_hold": 0.0}
            rows.append({
                "variety": v, "freq": freq, "n_trades": r.get("n_trades", 0),
                "pf": (r.get("pf", 0.0) if __import__("numpy").isfinite(r.get("pf", 0.0)) else None),
                "net_yuan": r.get("total_net_yuan", 0.0),
                "win_rate": r.get("win_rate", 0.0),
                "max_dd": r.get("max_dd", 0.0),
                "sharpe": r.get("sharpe", 0.0),
                "avg_hold": r.get("avg_hold", 0.0),
            })
            print(f"  {v}/{freq}: trades={r.get('n_trades',0)} "
                  f"pf={r.get('pf',0):.2f} net={r.get('total_net_yuan',0):.0f}")

    df = pd.DataFrame(rows)
    os.makedirs(out_dir, exist_ok=True)
    df.to_csv(os.path.join(out_dir, "deep_dive_tf.csv"), index=False)
    _summary(df, out_dir)
    print(f"[deep_dive] 完成 → {out_dir}/deep_dive_tf.csv ({len(df)} 行)")
    return df


def _summary(df: pd.DataFrame, out_dir: str) -> None:
    lines = ["# level_touch 分钟级深挖（信号密度假设验证）", ""]
    lines.append("品种均来自白名单强边缘；配置：allow_short=True, z=2.0, "
                 "stop=2.0ATR, target=3.0ATR, s3_p=0.6")
    lines.append("")
    lines.append("## 信号密度（n_trades）随频率变化")
    for v, g in df.groupby("variety"):
        g = g.set_index("freq")
        cells = []
        for f in FREQS:
            if f in g.index:
                cells.append(f"{f}={int(g.loc[f,'n_trades'])}")
            else:
                cells.append(f"{f}=-")
        lines.append(f"- {v}: " + " / ".join(cells))
    lines.append("")
    lines.append("## PF/净利 随频率变化")
    for v, g in df.groupby("variety"):
        g = g.set_index("freq")
        cells = []
        for f in FREQS:
            if f in g.index:
                cells.append(f"{f}:PF={g.loc[f,'pf']:.2f},net={g.loc[f,'net_yuan']:,.0f}")
            else:
                cells.append(f"{f}:-")
        lines.append(f"- {v}: " + " | ".join(cells))
    lines.append("")
    lines.append("## 结论")
    # 简单判断：15m 或 60m 的 n_trades 是否 > daily
    ratios = []
    for v, g in df.groupby("variety"):
        g = g.set_index("freq")
        if "daily" in g.index and "15m" in g.index:
            ratios.append(g.loc["15m", "n_trades"] / max(1, g.loc["daily", "n_trades"]))
    if ratios:
        lines.append(f"- 15m/daily 信号密度倍数 中位≈{pd.Series(ratios).median():.1f}×")
        lines.append("  （>1 说明高频确实提供更高信号密度，但需结合 PF 看边缘是否被稀释）")

    with open(os.path.join(out_dir, "deep_dive_tf.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    deep_dive()
