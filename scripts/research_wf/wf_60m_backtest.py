# -*- coding: utf-8 -*-
"""walk-forward @ 60m —— 容器运行副本（绝对导入，自带 NaN 兜底）。

仅对 RU、SC 在 60m 周期重做扩展窗 walk-forward，验证它们日线 walk-forward
翻车/边缘存疑，但分钟级深挖翻正的现象，是否跨样本外稳健。

逻辑与 app.backtest.level_touch.walk_forward 一致（此处为可部署副本，
因容器 /app/app 为只读挂载，无法直接更新包内模块）。

输出：/app/qh/level_touch_wf_60m/{wf_results.csv, wf_summary.md}
"""
from __future__ import annotations

import itertools
import os
from dataclasses import replace

import numpy as np
import pandas as pd

from app.backtest.level_touch.config import (
    DEFAULT_CONFIG, LevelTouchConfig, resolve_freq)
from app.backtest.level_touch.backtest import run_backtest


# 训练/测试切分（扩展窗，与日线 walk-forward 一致，便于对照）
_FOLDS = [
    ("F1", "2019-12-31", "2020-01-01", "2021-12-31"),
    ("F2", "2021-12-31", "2022-01-01", "2023-12-31"),
    ("F3", "2023-12-31", "2024-01-01", "2099-12-31"),
]

# 样本内选参网格（与日线一致）
_GRID = {
    "allow_short": [False, True],
    "support_z_thr": [1.5, 2.0, 2.5],
    "stop_atr_mult": [2.0, 3.0],
    "target_atr_mult": [3.0, 4.0],
    "s3_no_return_p_thr": [0.6, 0.7],
}

FREQ = "60m"
OUT_DIR = "/app/qh/level_touch_wf_60m"
VARIETIES = ["RU", "SC"]


def _load_full(variety, cfg_base):
    from app.data import barstore
    from app.data.barstore import variety_spec
    from datetime import date
    fr = resolve_freq(cfg_base)
    bars = barstore.load(variety, fr["bs_freq"], "continuous",
                         cfg_base.start, cfg_base.end)
    if bars.empty or len(bars) < fr["warmup_bars"] + 5:
        return None, None, None
    # NaN 兜底（容器旧版 backtest 无此清洗；60m 数据常含 NaN 行）
    bars = bars.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    if len(bars) < fr["warmup_bars"] + 5:
        return None, None, None
    # 时区统一：60m 的 ts 为 UTC 感知，日线为 naive；统一去时区，避免切片比较报错
    if getattr(bars["ts"].dt, "tz", None) is not None:
        bars["ts"] = bars["ts"].dt.tz_localize(None)
    try:
        spec = variety_spec(variety, as_of=date.today())
        tick = float(spec["tick_size"])
        mult = float(spec["multiplier"])
    except Exception:
        return None, None, None
    return bars, tick, mult


def _summarize_trades(trades, multiplier, initial=1_000_000.0):
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


def walk_forward_60m():
    cfg_base = replace(DEFAULT_CONFIG, freq=FREQ)
    combos = [dict(zip(_GRID, vals)) for vals in itertools.product(*_GRID.values())]
    print(f"[wf60m] 品种 {len(VARIETIES)} × 折叠 3 × 组合 {len(combos)} "
          f"≈ {len(VARIETIES)*3*len(combos)} 次回测 (freq={FREQ})")
    keys = list(_GRID)
    rows = []
    for vi, v in enumerate(VARIETIES, 1):
        bars, tick, mult = _load_full(v, cfg_base)
        if bars is None:
            print(f"  [{vi}/{len(VARIETIES)}] {v}: 数据不足，跳过")
            continue
        n_bars = len(bars)
        for fkey, train_end, test_start, test_end in _FOLDS:
            train_bars = bars[bars["ts"] <= pd.Timestamp(train_end)]
            if len(train_bars) < resolve_freq(cfg_base)["warmup_bars"] + 5:
                continue
            best, best_is, no_edge = None, None, True
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
            if best is None:
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
            oos_full = run_backtest(bars, v, replace(cfg_base, **best), tick, mult)
            test_trades = [t for t in oos_full["trades"]
                           if test_start <= t["entry_date"] <= test_end]
            oos = _summarize_trades(test_trades, mult)
            is_metrics = dict(n=best_is["n_trades"], pf=best_is["pf"],
                              net=best_is["total_net_yuan"], win=best_is["win_rate"])
            rows.append({
                "variety": v, "fold": fkey, "freq": FREQ,
                **{k: best[k] for k in keys},
                "is_n": is_metrics["n"], "is_pf": is_metrics["pf"],
                "is_net": is_metrics["net"], "is_win": is_metrics["win"],
                "oos_n": oos["n"], "oos_pf": oos["pf"], "oos_net": oos["net"],
                "oos_win": oos["win"], "oos_max_dd": oos["max_dd"],
                "oos_sharpe": oos["sharpe"], "no_is_edge": no_edge,
                "n_bars": n_bars,
            })
            print(f"  {v}/{fkey}: IS PF={is_metrics['pf']:.2f} "
                  f"OOS PF={oos['pf'] if np.isfinite(oos['pf']) else float('inf'):.2f} "
                  f"OOS n={oos['n']} net={oos['net']:,.0f}")
        print(f"  [{vi}/{len(VARIETIES)}] {v} 完成")

    df = pd.DataFrame(rows)
    os.makedirs(OUT_DIR, exist_ok=True)
    df.to_csv(os.path.join(OUT_DIR, "wf_results.csv"), index=False)
    _summary(df, OUT_DIR, keys)
    print(f"[wf60m] 完成 → {OUT_DIR}/wf_results.csv ({len(df)} 行)")
    return df


def _summary(df, out_dir, keys):
    lines = ["# level_touch walk-forward @ 60m（RU/SC 样本外验证）", ""]
    lines.append(f"品种：{df['variety'].nunique()} 个（RU, SC）× "
                 f"{df['fold'].nunique()} 折叠 = {len(df)} 行  周期=60m")
    lines.append("")
    oos_pos = df[df.oos_n >= 5]
    if len(oos_pos):
        lines.append(f"## 有 OOS 成交(>=5笔) 的折叠 {len(oos_pos)} 个")
        lines.append(f"- OOS PF 中位：{oos_pos.oos_pf.median():.2f}")
        lines.append(f"- OOS 净利合计：{oos_pos.oos_net.sum():,.0f} 元")
        lines.append(f"- OOS PF>1 占比：{(oos_pos.oos_pf>1).mean():.0%}")
        lines.append(f"- OOS 胜率中位：{oos_pos.oos_win.median():.0%}")
    lines.append("")
    lines.append("## IS→OOS 衰减")
    decay = oos_pos.copy()
    decay["pf_decay"] = decay.oos_pf - decay.is_pf
    lines.append(f"- IS PF 中位：{decay.is_pf.median():.2f} → "
                 f"OOS PF 中位：{decay.oos_pf.median():.2f}")
    lines.append(f"- 平均 PF 衰减：{decay.pf_decay.mean():.2f}")
    lines.append("")
    lines.append("## 无样本内边缘（no_is_edge=True，需警惕）")
    ne = df[df.no_is_edge]
    lines.append("- " + ", ".join(f"{r.variety}/{r.fold}" for r in ne.itertuples())
                 if len(ne) else "（无）")
    lines.append("")
    lines.append("## 各品种 OOS 汇总")
    for v, g in df.groupby("variety"):
        on = g[g.oos_n >= 5]
        if on.empty:
            continue
        lines.append(f"- {v}: OOS PF中位={on.oos_pf.median():.2f} "
                     f"net合计={on.oos_net.sum():,.0f} "
                     f"PF>1占比={(on.oos_pf>1).mean():.0%}")
    with open(os.path.join(out_dir, "wf_summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    walk_forward_60m()
