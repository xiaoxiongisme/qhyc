# -*- coding: utf-8 -*-
"""level_touch · 参数扫描（敏感度 / 最优配置搜索）。

对每个品种只加载一次 bars，遍历参数网格跑 run_backtest，输出：
  * <out>/sweep_results.csv      每行 = (品种 × 参数组合) 的回测指标
  * <out>/sweep_summary.md       每品种最优组合 + 整体稳健性结论

设计要点
--------
* 网格按键为 LevelTouchConfig 字段名（freq / allow_short / support_z_thr /
  stop_atr_mult / target_atr_mult / s3_no_return_p_thr …），用 dataclasses.replace
  生成变体，原默认配置不变。
* bars 按 (品种, freq) 缓存，避免每个组合重复取数。
* 分钟级(freq=60m/15m)单次回测较慢，建议先用小网格 + 少量品种探索，再放大。

在容器内运行：
  docker exec qhyc-api PYTHONPATH=/app python -m app.backtest.level_touch.sweep
"""
from __future__ import annotations

import itertools
import os
from dataclasses import replace
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

from .backtest import run_backtest
from .config import DEFAULT_CONFIG, LevelTouchConfig, resolve_freq

# bars 缓存：(variety, freq) -> (bars, tick, multiplier) | (None,*,*)
_BAR_CACHE: dict = {}


def default_grid() -> dict:
    """默认参数网格（可调）。键必须是 LevelTouchConfig 的字段名。"""
    return {
        "freq": ["daily", "60m"],
        "allow_short": [True, False],
        "support_z_thr": [1.5, 2.0, 2.5],
        "stop_atr_mult": [1.5, 2.0, 3.0],
        "target_atr_mult": [2.0, 3.0, 4.0],
        "s3_no_return_p_thr": [0.5, 0.6, 0.7],
    }


def _load_cached(variety: str, freq: str, cfg_base: LevelTouchConfig):
    key = (variety, freq)
    if key in _BAR_CACHE:
        return _BAR_CACHE[key]
    from app.data import barstore
    from app.data.barstore import variety_spec
    c = replace(cfg_base, freq=freq)
    fr = resolve_freq(c)
    bars = barstore.load(variety, fr["bs_freq"], "continuous",
                         cfg_base.start, cfg_base.end)
    val = (None, None, None)
    if not bars.empty and len(bars) >= fr["warmup_bars"] + 5:
        try:
            spec = variety_spec(variety, as_of=date.today())
            tick = float(spec["tick_size"])
            mult = float(spec["multiplier"])
            val = (bars, tick, mult)
        except Exception:
            val = (None, None, None)
    _BAR_CACHE[key] = val
    return val


def sweep(varieties: list, cfg_base: Optional[LevelTouchConfig] = None,
          grid: Optional[dict] = None,
          out_dir: str = "/app/qh/level_touch_out") -> pd.DataFrame:
    cfg_base = cfg_base or DEFAULT_CONFIG
    grid = grid or default_grid()
    keys = list(grid)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*grid.values())]
    print(f"[sweep] 品种 {len(varieties)} × 组合 {len(combos)} = "
          f"{len(varieties)*len(combos)} 次回测")

    rows = []
    for vi, v in enumerate(varieties, 1):
        for ci, combo in enumerate(combos):
            bars, tick, mult = _load_cached(v, combo.get("freq", "daily"), cfg_base)
            if bars is None:
                continue
            c = replace(cfg_base, **combo)
            try:
                res = run_backtest(bars, v, c, tick, mult)
            except Exception as e:  # noqa: BLE001
                res = {"n_trades": 0, "pf": 0.0, "total_net_yuan": 0.0,
                       "max_dd": 0.0, "sharpe": 0.0, "win_rate": 0.0,
                       "avg_hold": 0.0}
            rows.append({
                "variety": v,
                **combo,
                "n_trades": res.get("n_trades", 0),
                "pf": (res.get("pf", 0.0) if np.isfinite(res.get("pf", 0.0)) else None),
                "net_yuan": res.get("total_net_yuan", 0.0),
                "max_dd": res.get("max_dd", 0.0),
                "sharpe": res.get("sharpe", 0.0),
                "win_rate": res.get("win_rate", 0.0),
                "avg_hold": res.get("avg_hold", 0.0),
            })
        if vi % 5 == 0:
            print(f"  [{vi}/{len(varieties)}] {v} 完成")

    df = pd.DataFrame(rows)
    os.makedirs(out_dir, exist_ok=True)
    df.to_csv(os.path.join(out_dir, "sweep_results.csv"), index=False)
    _summary(df, out_dir, keys)
    print(f"[sweep] 完成 → {out_dir}/sweep_results.csv")
    return df


def _summary(df: pd.DataFrame, out_dir: str, grid_keys: list) -> None:
    lines = ["# level_touch 参数扫描汇总", ""]
    lines.append(f"网格维度：{', '.join(grid_keys)}")
    lines.append(f"样本：{df['variety'].nunique()} 品种 × "
                 f"{len(df)} (品种×组合) 行")
    lines.append("")

    # 每品种最优（按净利，要求至少 10 笔成交）
    lines.append("## 每品种最优组合（净利最大，且 n_trades>=10）")
    best_rows = []
    for v, g in df.groupby("variety"):
        gg = g[g.n_trades >= 10]
        if gg.empty:
            gg = g
        top = gg.sort_values("net_yuan", ascending=False).head(1)
        if not top.empty:
            r = top.iloc[0]
            best_rows.append(
                f"- {v}: net={r.net_yuan:,.0f} PF={r.pf} trades={int(r.n_trades)} "
                f"| " + " ".join(f"{k}={r[k]}" for k in grid_keys))
    lines.extend(best_rows or ["（无满足条件的品种）"])

    # 整体稳健性：各参数值的平均 PF / 盈利占比
    lines.append("")
    lines.append("## 各参数维度的盈利稳健性（按 n_trades>=10 子集）")
    sub = df[df.n_trades >= 10]
    for k in grid_keys:
        lines.append(f"### {k}")
        grp = sub.groupby(k).agg(
            n=("pf", "size"),
            pf_median=("pf", "median"),
            net_median=("net_yuan", "median"),
            win_share=("net_yuan", lambda s: (s > 0).mean()),
        )
        for idx, row in grp.iterrows():
            lines.append(
                f"- {idx}: n={int(row.n)} PF中位={row.pf_median:.2f} "
                f"净利中位={row.net_median:,.0f} 盈利占比={row.win_share:.0%}")

    with open(os.path.join(out_dir, "sweep_summary.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    # 默认跑一个代表性小集合，避免全市场 × 全网格过久
    sweep(["RB", "CU", "AU", "JM", "AG", "TA", "I", "MA"])
