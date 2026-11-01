# -*- coding: utf-8 -*-
"""扫描后处理：从 sweep_results.csv 推导选择性空头白名单 + 稳健性结论。

读取 sweep_out/sweep_results.csv：
  1) 逐品种对比 allow_short=False 与 =True 的最优组合(n_trades>=10)，
     找出「空头能改善 PF/净利」的品种 -> selective_short_whitelist.csv
  2) 各参数维度的盈利稳健性(中位 PF / 盈利占比)，按多头/多空分别统计
  3) 正期望强边缘组合清单(PF>1.2 & net>0 & n_trades>=20)
输出同目录：selective_short_whitelist.csv, sweep_analysis.md

在 qhyc-api 容器内运行（先跑 run_full_sweep.py）：
  docker exec -it qhyc-api env PYTHONPATH=/app \\
      python app/backtest/level_touch/report_sweep.py
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

OUT = "/app/qh/level_touch_sweep_out"
CSV = os.path.join(OUT, "sweep_results.csv")


def _as_bool(s):
    if isinstance(s, (bool, np.bool_)):
        return bool(s)
    return str(s).strip().lower() in ("true", "1", "yes")


def main() -> None:
    df = pd.read_csv(CSV)
    df["allow_short"] = df["allow_short"].map(_as_bool)
    df["n_trades"] = df["n_trades"].fillna(0).astype(int)
    df["net_yuan"] = df["net_yuan"].fillna(0.0)
    df["pf"] = pd.to_numeric(df["pf"], errors="coerce")

    sub = df[df["n_trades"] >= 10].copy()

    # 1) 选择性空头白名单
    rows = []
    for v, g in sub.groupby("variety"):
        gl = g[g["allow_short"] == False]
        gs = g[g["allow_short"] == True]
        long_best = gl.sort_values("net_yuan", ascending=False).head(1)
        short_best = gs.sort_values("net_yuan", ascending=False).head(1)
        if long_best.empty or short_best.empty:
            continue
        lb = long_best.iloc[0]
        sb = short_best.iloc[0]
        delta = sb["net_yuan"] - lb["net_yuan"]
        include = (delta > 0) and (pd.notna(sb["pf"]) and sb["pf"] >= 1.0) \
            and (sb["n_trades"] >= 20)
        rows.append({
            "variety": v,
            "long_net": round(lb["net_yuan"], 1),
            "long_pf": round(lb["pf"], 3) if pd.notna(lb["pf"]) else None,
            "long_trades": int(lb["n_trades"]),
            "short_net": round(sb["net_yuan"], 1),
            "short_pf": round(sb["pf"], 3) if pd.notna(sb["pf"]) else None,
            "short_trades": int(sb["n_trades"]),
            "delta_net": round(delta, 1),
            "include_short": include,
        })
    wl = pd.DataFrame(rows).sort_values("delta_net", ascending=False)
    wl.to_csv(os.path.join(OUT, "selective_short_whitelist.csv"), index=False)
    n_inc = int(wl["include_short"].sum())
    print(f"[report_sweep] 品种对比 {len(wl)} 个，建议开空头 {n_inc} 个")

    # 2) 各参数维度稳健性(多/空分别)
    dims = ["support_z_thr", "stop_atr_mult", "target_atr_mult",
            "s3_no_return_p_thr"]
    dim_md = []
    for label, mask in [("多头(allow_short=False)", sub["allow_short"] == False),
                        ("多空(allow_short=True)", sub["allow_short"] == True)]:
        dim_md.append(f"### {label}")
        for k in dims:
            grp = sub[mask].groupby(k).agg(
                n=("pf", "size"),
                pf_median=("pf", "median"),
                net_median=("net_yuan", "median"),
                win_share=("net_yuan", lambda s: (s > 0).mean()),
            )
            lines = [f"#### {k}"]
            for idx, r in grp.iterrows():
                lines.append(
                    f"- {idx}: n={int(r.n)} PF中位={r.pf_median:.2f} "
                    f"净利中位={r.net_median:,.0f} 盈利占比={r.win_share:.0%}")
            dim_md.append("\n".join(lines))
        dim_md.append("")

    # 3) 正期望强边缘组合
    edge = sub[(sub["pf"] > 1.2) & (sub["net_yuan"] > 0)
               & (sub["n_trades"] >= 20)].copy()
    edge = edge.sort_values(["pf", "net_yuan"], ascending=False)
    edge_cols = ["variety", "allow_short", "support_z_thr", "stop_atr_mult",
                 "target_atr_mult", "s3_no_return_p_thr", "n_trades", "pf",
                 "net_yuan", "max_dd", "sharpe"]
    edge_out = edge[edge_cols].head(60)

    md = []
    md.append("# level_touch 扫描后分析\n")
    md.append(f"- 扫描样本：{df['variety'].nunique()} 品种 × "
              f"{len(df)} (品种×组合) 行")
    md.append(f"- 有效组合(n_trades>=10)：{len(sub)} 行\n")
    md.append("## 1. 选择性空头白名单(建议开空头的品种)\n")
    if n_inc > 0:
        inc = wl[wl["include_short"]].copy()
        md.append(f"共 **{n_inc}** 个品种在「开空头」下净利/PF 显著优于纯多头：\n")
        for _, r in inc.iterrows():
            md.append(
                f"- {r['variety']}: 多头 net={r['long_net']:,.0f}(PF={r['long_pf']}) "
                f"→ 多空 net={r['short_net']:,.0f}(PF={r['short_pf']}) "
                f"Δ={r['delta_net']:,.0f}, 多空 trades={r['short_trades']}")
    else:
        md.append("（无品种满足「开空头显著更优」的阈值）")
    md.append("\n## 2. 各参数维度盈利稳健性\n")
    md.extend(dim_md)
    md.append("## 3. 正期望强边缘组合(PF>1.2 & net>0 & trades>=20)\n")
    md.append(f"命中 {len(edge_out)} 个组合，取前 60：\n")
    if not edge_out.empty:
        md.append(edge_out.to_markdown(index=False))
    md.append("")

    with open(os.path.join(OUT, "sweep_analysis.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"[report_sweep] -> {OUT}/sweep_analysis.md, "
          f"selective_short_whitelist.csv")


if __name__ == "__main__":
    main()
