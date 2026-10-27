# -*- coding: utf-8 -*-
"""level_touch · walk-forward 样本外验证（防止样本内选优过拟合）。

动机：全市场扫描 sweep_results.csv 是『全部 9558 组合里挑最优』，存在严重过拟合
风险。本脚本对白名单品种做扩展窗 walk-forward：
  训练窗（仅训练数据）选最优参数 → 测试窗（全历史回测后按 entry_date 过滤）验证。

切分（扩展窗，测试窗互不重叠）：
  F1: 训练≤2019-12-31, 测试 2020-01-01..2021-12-31
  F2: 训练≤2021-12-31, 测试 2022-01-01..2023-12-31
  F3: 训练≤2023-12-31, 测试 2024-01-01..未来

选参准则：训练窗内 PF>=1.0 且 n_trades>=10 的组合里取净利最大；若无满足 PF>=1 的，
则取 n_trades>=5 里净利最大，并标 flag no_is_edge=True（该品种/窗无样本内边缘）。

OOS 指标：测试窗内逐笔 trade 的 PF / 净利 / 胜率 / 最大回撤 / 夏普（由 trade 列表重算，
避免前视：训练选参只用训练段，测试评估用全历史跑但只统计测试窗成交）。

在容器内运行：
  docker exec qhyc-api PYTHONPATH=/app python -m app.backtest.level_touch.walk_forward
"""
from __future__ import annotations

import itertools
import os
from dataclasses import replace
from typing import Optional

import numpy as np
import pandas as pd

from .config import DEFAULT_CONFIG, LevelTouchConfig, resolve_freq
from .backtest import run_backtest
from .variety_config import WHITELIST_SHORT


# 训练/测试切分（扩展窗）
_FOLDS = [
    ("F1", "2019-12-31", "2020-01-01", "2021-12-31"),
    ("F2", "2021-12-31", "2022-01-01", "2023-12-31"),
    ("F3", "2023-12-31", "2024-01-01", "2099-12-31"),
]

# 样本内选参网格（比全扫描小，控制耗时）
_GRID = {
    "allow_short": [False, True],
    "support_z_thr": [1.5, 2.0, 2.5],
    "stop_atr_mult": [2.0, 3.0],
    "target_atr_mult": [3.0, 4.0],
    "s3_no_return_p_thr": [0.6, 0.7],
}


def _load_full(variety: str, cfg_base: LevelTouchConfig):
    from app.data import barstore
    from app.data.barstore import variety_spec
    from datetime import date
    fr = resolve_freq(cfg_base)
    bars = barstore.load(variety, fr["bs_freq"], "continuous",
                         cfg_base.start, cfg_base.end)
    if bars.empty or len(bars) < fr["warmup_bars"] + 5:
        return None, None, None
    # 清洗：分钟级数据偶含 NaN 行（缺失/异常），否则 lows.min()/highs.max()
    # 变 NaN 会让 make_grid 的 int(NaN) 崩溃。统一兜底（容器旧版 backtest 无此清洗）。
    bars = bars.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    if len(bars) < fr["warmup_bars"] + 5:
        return None, None, None
    # 时区统一：60m 的 ts 常为 UTC 感知、日线为 naive，统一去时区避免切片比较报错
    if getattr(bars["ts"].dt, "tz", None) is not None:
        bars["ts"] = bars["ts"].dt.tz_localize(None)
    try:
        spec = variety_spec(variety, as_of=date.today())
        tick = float(spec["tick_size"])
        mult = float(spec["multiplier"])
    except Exception:
        return None, None, None
    return bars, tick, mult


def _summarize_trades(trades: list, multiplier: float,
                      initial: float = 1_000_000.0) -> dict:
    if not trades:
        return dict(n=0, pf=0.0, net=0.0, win=0.0, max_dd=0.0, sharpe=0.0)
    net_list = np.array([t["net"] for t in trades], dtype=float)
    wins = net_list[net_list > 0]
    losses = net_list[net_list < 0]
    gw = float(wins.sum()) if len(wins) else 0.0
    gl = float(-losses.sum()) if len(losses) else 0.0
    pf = gw / gl if gl > 0 else float("inf")
    equity = initial + np.cumsum(net_list) * multiplier
    peak = np.maximum.accumulate(equity)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, (equity - peak) / peak, 0.0)
    max_dd = float(dd.min()) if np.isfinite(dd).any() else 0.0
    d = np.diff(equity)
    sharpe = float(np.mean(d) / d.std() * np.sqrt(252)) if d.std() > 0 else 0.0
    return dict(n=len(trades), pf=pf, net=float(net_list.sum() * multiplier),
                win=float(len(wins) / len(net_list)), max_dd=max_dd, sharpe=sharpe)


def walk_forward(varieties: list, cfg_base: Optional[LevelTouchConfig] = None,
                 out_dir: str = "/app/qh/level_touch_wf",
                 freq: str = "daily") -> pd.DataFrame:
    cfg_base = replace(cfg_base or DEFAULT_CONFIG, freq=freq)
    combos = [dict(zip(_GRID, vals))
              for vals in itertools.product(*_GRID.values())]
    print(f"[wf] 品种 {len(varieties)} × 折叠 3 × 组合 {len(combos)} "
          f"≈ {len(varieties)*3*len(combos)} 次回测")

    keys = list(_GRID)
    rows = []
    for vi, v in enumerate(varieties, 1):
        bars, tick, mult = _load_full(v, cfg_base)
        if bars is None:
            print(f"  [{vi}/{len(varieties)}] {v}: 数据不足，跳过")
            continue
        for fkey, train_end, test_start, test_end in _FOLDS:
            train_bars = bars[bars["ts"] <= pd.Timestamp(train_end)]
            if len(train_bars) < resolve_freq(cfg_base)["warmup_bars"] + 5:
                continue

            # ---- 训练窗选参 ----
            best = None
            best_is = None
            no_edge = True
            for combo in combos:
                c = replace(cfg_base, **combo)
                try:
                    r = run_backtest(train_bars, v, c, tick, mult)
                except Exception:
                    continue
                if r["n_trades"] >= 10 and np.isfinite(r["pf"]) and r["pf"] >= 1.0:
                    no_edge = False
                    if best is None or r["total_net_yuan"] > best_is["total_net_yuan"]:
                        best, best_is = combo, r
            if best is None:  # 无 PF>=1，退而求其次取 n>=5 净利最大
                for combo in combos:
                    c = replace(cfg_base, **combo)
                    try:
                        r = run_backtest(train_bars, v, c, tick, mult)
                    except Exception:
                        continue
                    if r["n_trades"] >= 5 and (best is None or
                                              r["total_net_yuan"] > best_is["total_net_yuan"]):
                        best, best_is = combo, r

            if best is None:
                continue

            # ---- 测试窗评估（全历史跑，仅统计测试窗成交）----
            oos_full = run_backtest(bars, v, replace(cfg_base, **best), tick, mult)
            test_trades = [t for t in oos_full["trades"]
                           if test_start <= t["entry_date"] <= test_end]
            oos = _summarize_trades(test_trades, mult)

            is_s = _summarize_trades(
                [t for t in oos_full["trades"]
                 if t["entry_date"] <= train_end], mult) \
                if False else None  # 占位，IS 直接用 best_is
            is_metrics = dict(n=best_is["n_trades"], pf=best_is["pf"],
                              net=best_is["total_net_yuan"],
                              win=best_is["win_rate"])

            rows.append({
                "variety": v, "fold": fkey,
                **{k: best[k] for k in keys},
                "is_n": is_metrics["n"], "is_pf": is_metrics["pf"],
                "is_net": is_metrics["net"], "is_win": is_metrics["win"],
                "oos_n": oos["n"], "oos_pf": oos["pf"], "oos_net": oos["net"],
                "oos_win": oos["win"], "oos_max_dd": oos["max_dd"],
                "oos_sharpe": oos["sharpe"],
                "no_is_edge": no_edge,
            })
        if vi % 5 == 0:
            print(f"  [{vi}/{len(varieties)}] {v} 完成")

    df = pd.DataFrame(rows)
    os.makedirs(out_dir, exist_ok=True)
    df.to_csv(os.path.join(out_dir, "wf_results.csv"), index=False)
    _summary(df, out_dir, keys)
    print(f"[wf] 完成 → {out_dir}/wf_results.csv ({len(df)} 行)")
    return df


def _summary(df: pd.DataFrame, out_dir: str, keys: list) -> None:
    lines = ["# level_touch walk-forward 样本外验证", ""]
    lines.append(f"样本：{df['variety'].nunique()} 品种 × "
                 f"{df['fold'].nunique()} 折叠 = {len(df)} 行")
    lines.append("")
    # 整体 OOS 表现
    oos_pos = df[df.oos_n >= 5]
    if len(oos_pos):
        lines.append(f"## 有 OOS 成交(>=5笔) 的折叠 {len(oos_pos)} 个")
        lines.append(f"- OOS PF 中位：{oos_pos.oos_pf.median():.2f}")
        lines.append(f"- OOS 净利合计：{oos_pos.oos_net.sum():,.0f} 元")
        lines.append(f"- OOS PF>1 占比：{(oos_pos.oos_pf>1).mean():.0%}")
        lines.append(f"- OOS 胜率中位：{oos_pos.oos_win.median():.0%}")
    lines.append("")
    lines.append("## IS→OOS 衰减（过拟合信号）")
    decay = oos_pos.copy()
    decay["pf_decay"] = decay.oos_pf - decay.is_pf
    lines.append(f"- IS PF 中位：{decay.is_pf.median():.2f} → "
                 f"OOS PF 中位：{decay.oos_pf.median():.2f}")
    lines.append(f"- 平均 PF 衰减：{decay.pf_decay.mean():.2f}")
    lines.append("")
    lines.append("## 无样本内边缘的品种/窗（no_is_edge=True，需警惕）")
    ne = df[df.no_is_edge]
    if len(ne):
        lines.append("- " + ", ".join(
            f"{r.variety}/{r.fold}" for r in ne.itertuples()))
    else:
        lines.append("（无）")
    lines.append("")
    lines.append("## 各品种 OOS 汇总（按折叠聚合）")
    for v, g in df.groupby("variety"):
        on = g[g.oos_n >= 5]
        if on.empty:
            continue
        lines.append(f"- {v}: OOS PF中位={on.oos_pf.median():.2f} "
                     f"net合计={on.oos_net.sum():,.0f} "
                     f"PF>1占比={(on.oos_pf>1).mean():.0%} "
                     f"({'空头' if v in WHITELIST_SHORT else '多头'})")

    with open(os.path.join(out_dir, "wf_summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    # 用法：python -m app.backtest.level_touch.walk_forward [freq] [variety ...]
    #   freq 默认 daily；variety 默认对白名单 22 个做全量（耗时可控）
    import sys
    freq = sys.argv[1] if len(sys.argv) > 1 else "daily"
    vs = sys.argv[2:] if len(sys.argv) > 2 else sorted(WHITELIST_SHORT)
    walk_forward(vs, freq=freq)
