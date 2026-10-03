# -*- coding: utf-8 -*-
"""方向3 追加：为什么"低成本品种池"反而更差？分解验证。

STEP3 的决定性数字：随机20品种 +25.4万 > 最低成本20品种 -27.0万（好 52 万）
→ 成本占比与收益【反向】，不符合常理，必须拆解。

三个待检假设
  H1 容量挤出：低成本池笔数不降（1091 vs 全池1226）→ 低成本品种因止损窄而信号密，
     占满 cap=5 的仓位，把好品种挤出组合
  H2 成本占比误导：占比低≠成本低。占比=cost/(2*ATR)，分母大也会让占比低。
     L/V/PP 是"绝对成本低"（真便宜），AU/I/J 是"分母小"（幅度太小）——两类完全不同
  H3 低波动品种无趋势 alpha：成本不是瓶颈，品种本身没趋势才是
"""
import os
import sys
import importlib.util

import numpy as np
import pandas as pd

sys.path.insert(0, r"E:\Docker\qhyc")
sys.argv = ["x"]

_spec = importlib.util.spec_from_file_location(
    "pl", r"E:\Docker\qhyc\scripts\validate\pool_lowcost_4h.py")
pl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pl)

c4 = pl.c4
from app.backtest.fusion_backtest import FusionBacktestParams  # noqa: E402
from app.strategies.fusion_signal import atr14  # noqa: E402

OUT = pl.OUT
IS = pl.IS


def safe_ratio(cost_sum, gross_sum):
    """成本/毛利：毛利<=0 时无意义（此前出现 8e15 荒谬值），改用可读标记。"""
    if gross_sum <= 0:
        return float("nan")
    return 100.0 * cost_sum / gross_sum


def main():
    hb = c4.load_hourly()
    bars4 = {}
    for s, b in hb.items():
        nb, _ = c4.to_4h(b)
        if len(nb["c"]) >= 500:
            bars4[s] = nb

    p = FusionBacktestParams()
    p.min_bars = 60
    p.cooldown_bars = 1
    p.W = 20
    p.ema_k = 20
    p.ma_n = 10

    CT = pl.cost_ratio_table(bars4, p)
    CT = CT.rename(columns={"atr_med": "atr", "amp_med": "amp2atr"})
    # 拆解：占比 = 绝对成本 / (2*ATR)。区分"真便宜"与"分母小"
    CT["cost_bucket"] = pd.cut(CT.cost1, [0, 30, 60, 120, 1e6],
                               labels=["<=30元", "30-60元", "60-120元", ">120元"])
    print("=== 成本占比的构成拆解（占比 = 绝对成本 / 2*ATR）===")
    print("%-10s %10s %10s %8s %12s" % ("sym", "单笔成本", "2*ATR", "占比", "成本分档"))
    for _, r in CT.iterrows():
        print("%-10s %10.0f %10.1f %8.3f %12s"
              % (r.sym, r.cost1, r.amp2atr, r.ratio, r.cost_bucket))
    print()

    # ---------------- H2：按【绝对成本】而非【占比】分池
    print("=== H2 检验：按【绝对成本】选池（而非占比）===")
    CT_by_cost = CT.sort_values("cost1").reset_index(drop=True)
    allp = set(bars4.keys())
    for n in [20, 30]:
        pool = set(CT_by_cost.head(n).sym)
        r = pl.run_pool(bars4, p, pool, f"绝对成本最低 {n} 品种")
    print()

    # ---------------- H1：容量挤出检验
    print("=== H1 检验：容量挤出（把 cap 从 5 放大到 50）===")
    for tag, pool in [("全池", allp),
                      ("最低成本20", set(CT.head(20).sym)),
                      ("最低成本30", set(CT.head(30).sym))]:
        sub = {s: b for s, b in bars4.items() if s in pool}
        pos = c4.run_v34(sub, p)
        for cap in (5, 20, 50):
            rr = c4.replay(pos, cap=cap)
            L = pd.DataFrame([dict(sym=x["p"]["sym"], Y=x["Y"],
                                   gross=x["gross"], cost=x["cost"])
                              for x in rr["rows"]])
            if len(L) == 0:
                continue
            d = pd.to_datetime([pd.Timestamp(x["p"]["entry_dt"]) for x in rr["rows"]])
            L["dt"] = d
            isv = L[L.dt <= IS].Y.sum()
            oosv = L[L.dt > IS].Y.sum()
            print("  %-12s cap=%2d n=%5d 净%+8.1f万 (毛利%+7.1f万 成本/毛利%6s) "
                  "IS%+7.1f OOS%+7.1f"
                  % (tag, cap, rr["executed"], rr["net"] / 1e4, L.gross.sum() / 1e4,
                     ("%.0f%%" % safe_ratio(L.cost.sum(), L.gross.sum()))
                     if L.gross.sum() > 0 else "毛利<=0",
                     isv / 1e4, oosv / 1e4))
    print()

    # ---------------- H3：低波动品种是否有趋势
    print("=== H3 检验：4h 单笔幅度 vs 该品种盈亏（趋势性）===")
    pos = c4.run_v34(bars4, p)
    rr = c4.replay(pos)
    rows = []
    for x in rr["rows"]:
        pr = x["p"]
        rows.append(dict(sym=pr["sym"], Y=x["Y"], gross=x["gross"], cost=x["cost"],
                         lots=len(pr["lot_pxs"])))
    L = pd.DataFrame(rows)
    per = L.groupby("sym").agg(n=("Y", "size"), Y=("Y", "sum"),
                               gross=("gross", "sum"), cost=("cost", "sum"))
    per = per.join(CT.set_index("sym")[["cost1", "amp2atr", "ratio"]])
    per["pen_gross"] = per.gross / per.cost
    per["pen_net"] = per.Y / per.cost
    per = per.sort_values("ratio")
    print("%-10s %6s %10s %8s %10s %10s %8s"
          % ("sym", "笔数", "净收益", "占比", "2*ATR", "毛利/成本", "净/成本"))
    for sym, r in per.iterrows():
        print("%-10s %6d %+10.1f万 %8.3f %10.1f %10.2f %8.2f"
              % (sym, r.n, r.Y / 1e4, r.ratio, r.amp2atr, r.pen_gross, r.pen_net))
    print()

    from scipy.stats import spearmanr
    rho_r, pv_r = spearmanr(per.index.map(lambda s: CT.set_index("sym").ratio[s]), per.Y)
    rho_c, pv_c = spearmanr(per.cost1, per.Y)
    rho_a, pv_a = spearmanr(per.amp2atr, per.Y)
    print("=== 决定性相关性（品种层面 n=%d）===" % len(per))
    print("  spearman(成本占比, 净收益)   = %+.3f (p=%.3f)" % (rho_r, pv_r))
    print("  spearman(绝对成本, 净收益)   = %+.3f (p=%.3f)" % (rho_c, pv_c))
    print("  spearman(2*ATR,   净收益)   = %+.3f (p=%.3f)" % (rho_a, pv_a))
    print()
    pen = per[per.n >= 20]
    rho_p, pv_p = spearmanr(pen.pen_gross, pen.Y)
    print("  仅看笔数>=20 的 %d 个品种:" % len(pen))
    print("  spearman(毛利/成本, 净收益)  = %+.3f (p=%.3f)" % (rho_p, pv_p))
    print()

    # ---------------- STEP 4 修正：按占比两端看单调性
    print("=== 单调性检验：成本占比五分档（每档 10 品种）===")
    print("%-12s %6s %10s %12s %10s" % ("档位", "笔数", "净收益", "毛利/成本", "占比范围"))
    qs = np.quantile(CT.ratio, [0, .2, .4, .6, .8, 1.0])
    for i in range(5):
        lo, hi = qs[i], qs[i + 1]
        pool = set(CT[(CT.ratio >= lo) & (CT.ratio <= hi if i == 4 else CT.ratio < hi)].sym)
        sub = {s: b for s, b in bars4.items() if s in pool}
        pp = c4.run_v34(sub, p)
        rr = c4.replay(pp)
        LL = pd.DataFrame([dict(Y=x["Y"], gross=x["gross"], cost=x["cost"])
                           for x in rr["rows"]])
        if len(LL) == 0:
            continue
        print("Q%d [%.2f,%.2f) %6d %+10.1f万 %12s %10s"
              % (i + 1, lo, hi, len(LL), LL.Y.sum() / 1e4,
                 ("%.2f" % (LL.gross.sum() / max(LL.cost.sum(), 1e-9))),
                 "[%.2f,%.2f]" % (lo, hi)))
    print()

    per.to_csv(os.path.join(OUT, "pool_diagnose.csv"))
    print("[out] pool_diagnose.csv")


if __name__ == "__main__":
    main()
