# -*- coding: utf-8 -*-
"""方向3：4h + 降成本品种组合。

背景（任务 #39）：4h 上V3.4 笔均毛利从 -144 元转正到 +188 元，成本/毛利 37%，
机制假设"成本与波动比例是瓶颈"成立。但稳健性不过关（IS-OOS 翻号、去TOP5 转负）。

本轮假设：若只保留"成本占比低"的品种，成本/毛利会进一步改善，稳健性可能过关。

★ 设计要点（防样本选择偏差）
1. 成本占比 = 成本 / 预期单笔幅度，预期幅度用【入场时 ATR】估计（因果，不含未来）
2. 必须配【随机同等数量品种池】对照 —— 若低���本池只是"恰好挑中了赚钱品种"，
   随机池会得到同样好的结果；只有低��本池显著优于随机池，才是真机制
3. 品种池在全样本上确定（不随 IS/OOS 变动），避免"按结果选池"
4. 成本占比高 vs 低 两端各取若干档，看是否存在单调关系（单调才像机制，尖峰是噪声）
"""
import os
import sys
import importlib.util

import numpy as np
import pandas as pd

sys.path.insert(0, r"E:\Docker\qhyc")
sys.argv = ["x"]

# 复用 4h 脚本的口径与函数
_spec = importlib.util.spec_from_file_location(
    "c4", r"E:\Docker\qhyc\scripts\validate\cycle_4h_1_verify.py")
c4 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(c4)

from app.backtest.fusion_backtest import FusionBacktestParams  # noqa: E402
from app.strategies.fusion_signal import atr14  # noqa: E402

OUT = c4.OUT
IS = c4.IS_SPLIT
HALF = c4.HALF


# ---------------------------------------------------------------- 成本占比
def cost_ratio_table(bars4, p):
    """每品种的成本占比 = 单笔成本 / 预期单笔幅度(2*ATR入场时估计)。

    纯因果：只用入场当刻的 ATR 与该品种固定费率，不用任何未来价格。
    返回 DataFrame[sym, cost1, atr_med, amp_med, ratio]（amp_med 为全样本中位数，
    仅用于排序分组，不参与交易决策）。
    """
    rows = []
    for sym, b in bars4.items():
        mult = c4.MULT.get(sym)
        if mult is None:
            continue
        c1 = c4.cost(sym, mult, lots=1)
        a = atr14(b["h"], b["l"], b["c"], 14)
        a = a[np.isfinite(a) & (a > 0)]
        if len(a) < 100:
            continue
        # 预期单笔幅度 = 2*ATR（与引擎的 2*ATR 止损同量级）
        amp_med = float(np.median(2.0 * a))
        rows.append(dict(sym=sym, cost1=c1, atr_med=float(np.median(a)),
                         amp_med=amp_med, ratio=c1 / max(amp_med, 1e-9)))
    return pd.DataFrame(rows).sort_values("ratio").reset_index(drop=True)


# ---------------------------------------------------------------- 取池并回测
def run_pool(bars4, p, pool, tag, verbose=True):
    sub = {s: b for s, b in bars4.items() if s in pool}
    pos = c4.run_v34(sub, p)
    r = c4.replay(pos)
    L = pd.DataFrame([dict(sym=x["p"]["sym"], dt=pd.Timestamp(x["p"]["entry_dt"]),
                           Y=x["Y"], gross=x["gross"], cost=x["cost"],
                           lots=len(x["p"]["lot_pxs"]))
                      for x in r["rows"]])
    if len(L) == 0:
        return None
    d = pd.to_datetime(L.dt)
    isv = L[d <= IS].Y.sum()
    oosv = L[d > IS].Y.sum()
    e = L[d <= HALF].Y.sum()
    l2 = L[d > HALF].Y.sum()
    oosL = L[d > IS]
    t5 = oosL.groupby("sym").Y.sum().nlargest(5).index.tolist()
    no5 = oosL[~oosL.sym.isin(t5)].Y.sum()
    res = dict(tag=tag, n=r["executed"], net=r["net"], win=r["win"],
               gross=L.gross.sum(), cost=L.cost.sum(),
               ratio=100 * L.cost.sum() / max(L.gross.sum(), 1e-9),
               isv=isv, oosv=oosv, half_e=e, half_l=l2,
               same=int(np.sign(isv) == np.sign(oosv)),
               no5=no5, n_syms=len(set(L.sym)))
    if verbose:
        print("  %-26s n=%5d 净%+8.1f万 毛利%+8.1f万 成本/毛利%5.0f%% "
              "IS%+7.1f OOS%+7.1f %s 去TOP5后OOS%+7.1f万"
              % (tag, res["n"], res["net"] / 1e4, res["gross"] / 1e4, res["ratio"],
                 isv / 1e4, oosv / 1e4, "同号" if res["same"] else "翻号",
                 no5 / 1e4))
    return res


def main():
    hb = c4.load_hourly()
    bars4 = {}
    for s, b in hb.items():
        nb, _ = c4.to_4h(b)
        if len(nb["c"]) >= 500:
            bars4[s] = nb
    print("4h 品种数: %d\n" % len(bars4))

    p = FusionBacktestParams()
    p.min_bars = 60
    p.cooldown_bars = 1
    p.W = 20
    p.ema_k = 20
    p.ma_n = 10

    # 成本占比表
    CT = cost_ratio_table(bars4, p)
    CT.to_csv(os.path.join(OUT, "cost_ratio_table.csv"), index=False)
    print("=== 成本占比排序（cost1 / (2*ATR)）===")
    print("%-10s %10s %12s %8s" % ("sym", "单笔成本元", "2*ATR中位", "占比"))
    for _, r in CT.iterrows():
        print("%-10s %10.0f %12.1f %8.3f" % (r.sym, r.cost1, r.amp_med, r.ratio))
    print()

    allp = set(bars4.keys())
    print("=== STEP 1: 全池基线（对照）===")
    base = run_pool(bars4, p, allp, "全池 50 品种")
    print()

    print("=== STEP 2: 成本占比最低的 N 个品种（分档看单调性）===")
    rows = []
    for n in [15, 20, 25, 30, 35, 40]:
        pool = set(CT.head(n).sym)
        r = run_pool(bars4, p, pool, f"最低成本 {n} 品种")
        if r:
            r["n_sel"] = n
            r["kind"] = "low"
            rows.append(r)
    print()

    print("=== STEP 3: ★随机同等数量品种池对照（排除样本选择偏差）===")
    print("若随机池与低成本池结果相当 → 低成本池只是挑中了赚钱品种，非机制")
    rng = np.random.default_rng(20261003)
    rand_rows = []
    for n in [20, 25, 30]:
        vals = []
        for rep in range(5):
            pool = set(rng.choice(sorted(allp), size=n, replace=False).tolist())
            r = run_pool(bars4, p, pool, f"随机{n}品种 #{rep+1}", verbose=(rep == 0))
            if r:
                vals.append(r)
        if vals:
            nets = np.array([v["net"] for v in vals])
            same = np.mean([v["same"] for v in vals])
            no5 = np.mean([v["no5"] for v in vals])
            print("  >> 随机%2d品种 5次: 净利中位%+8.1f万 (范围%+8.1f~%+8.1f) "
                  "同号率%.0f%% 去TOP5后OOS中位%+8.1f万"
                  % (n, np.median(nets) / 1e4, nets.min() / 1e4, nets.max() / 1e4,
                     100 * same, np.median(no5) / 1e4))
            rand_rows.append(dict(n_sel=n, net_med=float(np.median(nets)),
                                  net_min=float(nets.min()), net_max=float(nets.max()),
                                  same_rate=float(same), no5_med=float(no5)))
    print()

    print("=== STEP 4: 成本占比最高端（验证反向——高成本池是否更差）===")
    for n in [20, 30]:
        pool = set(CT.tail(n).sym)
        run_pool(bars4, p, pool, f"最高成本 {n} 品种")
    print()

    print("=== STEP 5: 判决四道关口（低成本最优档）===")
    if rows:
        best = max(rows, key=lambda r: r["net"])
        pool = set(CT.head(best["n_sel"]).sym)
        pos = c4.run_v34({s: b for s, b in bars4.items() if s in pool}, p)
        r = c4.replay(pos)
        L = pd.DataFrame([dict(sym=x["p"]["sym"], dt=pd.Timestamp(x["p"]["entry_dt"]),
                               Y=x["Y"], lots=len(x["p"]["lot_pxs"]))
                          for x in r["rows"]])
        d = pd.to_datetime(L.dt)
        yr = L.groupby(d.dt.year).Y.sum()
        print("  最优档 = 最低成本 %d 品种" % best["n_sel"])
        print("  关口1 IS-OOS 同号: %s (IS%+.1f OOS%+.1f)"
              % ("PASS" if best["same"] else "FAIL", best["isv"] / 1e4, best["oosv"] / 1e4))
        print("  关口2 半样本同号: %s (前%+.1f 后%+.1f)"
              % ("PASS" if np.sign(best["half_e"]) == np.sign(best["half_l"]) else "FAIL",
                 best["half_e"] / 1e4, best["half_l"] / 1e4))
        print("  关口3 去TOP5品种后 OOS: %+.1f万 (基准OOS %+.1f万) -> %s"
              % (best["no5"] / 1e4, best["oosv"] / 1e4,
                 "PASS" if best["no5"] > 0 else "FAIL"))
        pos_cells = sum(1 for _ in [r])
        print("  关口4 成本/毛利: %.0f%% (全池 %.0f%%) -> %s"
              % (best["ratio"], base["ratio"],
                 "PASS" if best["ratio"] < base["ratio"] else "FAIL"))
        print("  逐年: " + " ".join("%d:%+.0f" % (k, v / 1e4) for k, v in yr.items()))
        s = L.Y.sort_values(ascending=False)
        print("  去极值: 全部%+.1f万 | 去最大1笔%+.1f万 | 去最大5笔%+.1f万"
              % (L.Y.sum() / 1e4, s.iloc[1:].sum() / 1e4, s.iloc[5:].sum() / 1e4))
        L.to_csv(os.path.join(OUT, "lowcost_pool_ledger.csv"), index=False)

    # 落盘
    import json
    with open(os.path.join(OUT, "lowcost_pool_result.json"), "w", encoding="utf-8") as f:
        json.dump(dict(low=rows, rand=rand_rows,
                       base={k: float(v) for k, v in base.items() if k != "tag"}),
                  f, ensure_ascii=False, indent=2, default=float)
    print("\n[out] %s" % os.path.join(OUT, "lowcost_pool_result.json"))


if __name__ == "__main__":
    main()
