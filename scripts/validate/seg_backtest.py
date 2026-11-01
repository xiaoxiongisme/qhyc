# -*- coding: utf-8 -*-
"""分段回测：验证"毛利褪色"是真衰减，还是 akshare 只有 3-4 年样本的假象。

为什么不用 hourly_bar_uni 拼接（重要）
--------------------------------------
`bar_grid_probe.py` 实测两源 bar 网格不同构：
    agg_1m  2015-01~2023-09  日均 9.2 根，含 09:30 / 10:45 / 11:15 / 13:45 / 14:15 等非整点
    akshare 2020-02~2026-10  日均 7.2 根，规整整点
且 J1/J4 证明两源是**接力**（交界同日、跨源重叠 0），没有同窗口对照的可能。
于是一次回测里混用两段 = ema_k=140 的时间含义在交界点突变（15 交易日 → 19.4 交易日），
长周期结论不可信。

替代方案（本脚本）：**按 src 分段独立回测，各自段内 self-consistent**，
再用「分辨率折算」做敏感性检验：
    折算系数 = 9.2 / 7.2 ≈ 1.278  →  ema_k 140→179、atr_n 14→18
若原始参数与折算参数结论同向，则说明结论对 bar 分辨率不敏感，可下判断。

多配置一次跑完，支持 --src / --start / --end / --ema-k / --atr-n 覆盖。

跑法：
    set -a; . /home/ubuntu/qhyc_work/qhyc.env; set +a
    cd /Docker/qhyc && PYTHONPATH=/Docker/qhyc \
        /home/ubuntu/qhyc_work/venv/bin/python /tmp/seg_backtest.py
"""
from __future__ import annotations

import argparse
from datetime import date

import numpy as np

from app.core.db import session_scope
from app.backtest.fusion_backtest import (
    fusion_params_from_config,
    run_fusion_backtest,
)

CONFIGS = [
    # name,           src,       start,        end,          ema_k, atr_n
    ("A1 agg_1m 全段(原始参数)", "agg_1m", "2015-01-01", "2023-01-09", 140, 14),
    ("A2 agg_1m 净段(排早期脏数据)", "agg_1m", "2017-01-01", "2023-01-09", 140, 14),
    ("A3 agg_1m 净段(分辨率折算179/18)", "agg_1m", "2017-01-01", "2023-01-09", 179, 18),
    ("B1 akshare 近期(对照)", "akshare", "2023-01-01", "2026-10-30", 140, 14),
]


def money(x: float) -> str:
    return f"{x:+,.0f}"


def summarize(trades: list[dict], name: str, indent: str = "  ") -> dict:
    if not trades:
        print(f"{indent}{name}: 无样本")
        return {}
    mult = np.array([float(t["mult"]) for t in trades], float)
    pnl = np.array([float(t["pnl"]) for t in trades], float)
    cpt = np.array([float(t["cost_points"]) for t in trades], float)
    gross = (pnl + cpt) * mult
    cost = cpt * mult
    net = pnl * mult
    R = np.array([float(t["R"]) for t in trades], float)
    win = net > 0
    print(f"{indent}{name:<34} N={len(trades):>6}  毛={money(gross.sum()):>12}  "
          f"成本={money(cost.sum()):>11}  净={money(net.sum()):>12}  "
          f"胜率={win.mean()*100:>5.1f}%  毛R={R.mean():+.4f}")
    return {"n": len(trades), "gross": float(gross.sum()), "cost": float(cost.sum()),
            "net": float(net.sum()), "mean_R": float(R.mean()), "win": float(win.mean())}


def _yr(t) -> int:
    return t["entry_dt"].year


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只跑名字含该子串的配置")
    args = ap.parse_args()

    base = fusion_params_from_config()
    print(f"=== 基线生产参数 ema_k={base.ema_k} atr_n={base.atr_n} "
          f"sl={base.sl_atr} trail={base.trail_atr} be_r={base.be_r} "
          f"adx_min={base.adx_min} add_max_lots={base.add_max_lots} ===")

    for name, src, s0, s1, ema_k, atr_n in CONFIGS:
        if args.only and args.only not in name:
            continue
        p = fusion_params_from_config()
        p.src = src
        p.ema_k = ema_k
        p.atr_n = atr_n
        with session_scope() as sess:
            res = run_fusion_backtest(
                sess, symbols=None, params=p,
                start=date.fromisoformat(s0), end=date.fromisoformat(s1),
                include_trades=True,
            )
        trades = [t for t in res["trades"] if not t.get("open")]
        print(f"\n{'='*100}\n{name}  [{s0} ~ {s1}]  ema_k={ema_k} atr_n={atr_n}  "
              f"品种={len(res['per_symbol'])}\n{'='*100}")
        blk = summarize(trades, "全段")
        print("  —— 逐年 ——")
        for y in sorted({_yr(t) for t in trades}):
            summarize([t for t in trades if _yr(t) == y], str(y))
        if blk:
            print(f"  → 成本占毛 {blk['cost']/blk['gross']*100:.1f}%" if blk["gross"] else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
