"""
Step 7: 判决性终检 —— mr 族 343% 是真结构还是少数品种巧合？
================================================================
已确认的三个事实：
  1. gate 族 = mr 族（1160 笔逐位相同）→ 状态门控是恒等变换，无效，已废弃
  2. mr 族 IS最优 thr=0.06/exit=0.04/stop=0.10 -> IS +296.8% / OOS +46.2%
  3. 收益不靠极端值（去Top5仍 323%），但 TOP5 品种占正收益 74%

本步四道终检：
  F1 半样本拆分（前半 vs 后半参数是否一致）—— 防止"全样本最优"
  F2 去集中度：剔除 TOP5 贡献品种后重跑 OOS —— 防止"少数品种抬轿"
  F3 纯 OOS 参数面：IS 选出的参数在 OOS 的正收益比例（不是最优点，是整片）
  F4 走"相邻参数"而非最优点：参数高原检验（稳健平台 vs 尖峰）
  F5 基准对照：买入持有 / 随机入场同参数 —— 收益是否只是波动率的奖赏
"""
import os
import numpy as np
import pandas as pd
import psycopg2
import importlib.util

OUT = r"E:\Docker\qhyc\docs\_swing_out"
spec = importlib.util.spec_from_file_location(
    "v2", r"E:\Docker\qhyc\scripts\_swing_validate_2_trade.py")
V2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(V2)   # 复用其 load/backtest_family/stats

IS_END = pd.Timestamp("2021-12-31")
TOP5 = ["KQ.m@SHFE.fu", "KQ.m@DCE.j", "KQ.m@SHFE.hc", "KQ.m@CZCE.FG", "KQ.m@CZCE.CJ"]


def main():
    df = V2.load()
    # 需要旧参数基准：V2 的 COST_BP 全局
    V2.COST_BP = 2.0
    print(f"[data] rows={len(df)} syms={df.symbol.nunique()}")

    THRESH = [0.04, 0.05, 0.06, 0.07, 0.08, 0.10, 0.12]
    EXITS = [0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.12]
    STOPS = [0.05, 0.08, 0.10, 0.15]

    # ---------- 全网格（IS / OOS 分别统计）
    print("\n=== F3/F4 纯 OOS 参数高原检验：OOS 正收益比例 ===")
    rows = []
    cache = {}
    for thr in THRESH:
        for ex in EXITS:
            for st in STOPS:
                t = V2.backtest_family(df, "mr", thr, ex, st)
                if len(t) == 0:
                    continue
                key = (thr, ex, st)
                cache[key] = t
                is_t = t[t.period == "IS"]
                oos_t = t[t.period == "OOS"]
                rows.append(dict(thr=thr, exit=ex, stop=st, n=len(t),
                                 is_tot=is_t.net_pct.sum(), oos_tot=oos_t.net_pct.sum(),
                                 oos_n=len(oos_t),
                                 oos_pf=(oos_t.loc[oos_t.net_pct > 0, "net_pct"].sum() /
                                         max(-oos_t.loc[oos_t.net_pct < 0, "net_pct"].sum(), 1e-9)),
                                 all_tot=t.net_pct.sum()))
    G = pd.DataFrame(rows)
    G.to_csv(os.path.join(OUT, "f3_oos_grid.csv"), index=False)
    n = len(G)
    print(f"参数组数 {n}")
    print(f"  IS  正: {100*(G.is_tot>0).mean():.0f}%   OOS 正: {100*(G.oos_tot>0).mean():.0f}%   "
          f"同号: {100*(np.sign(G.is_tot)==np.sign(G.oos_tot)).mean():.0f}%")

    # OOS 最优（作弊参考：直接用 OOS 选参，不可用，只看天花板）
    print("\n--- OOS 直接选参（作弊天花板，仅参考）---")
    print(G.sort_values("oos_tot", ascending=False).head(5).to_string(index=False))

    # IS 选出的最优点，看它在 OOS 的表现
    bis = G.sort_values("is_tot", ascending=False).iloc[0]
    print(f"\n--- IS 最优点 thr={bis.thr} ex={bis.exit} st={bis.stop}: "
          f"IS {bis.is_tot:+.1f}% / OOS {bis.oos_tot:+.1f}% (n={bis.oos_n}, PF={bis.oos_pf:.2f}) "
          f"衰减 {100*(1-bis.oos_tot/max(bis.is_tot,1e-9)):.0f}%")

    # ---------- F4 参数高原：固定 thr 扫 exit×stop 的 OOS 曲面
    print("\n=== F4 参数高原（OOS 净收益 %，thr=0.06 切片）===")
    sl = G[G.thr == 0.06].pivot(index="stop", columns="exit", values="oos_tot")
    print(sl.round(1).to_string())
    pos = (sl > 0).sum().sum()
    print(f"  该切片 OOS 正收益格子: {pos}/{sl.size} = {100*pos/sl.size:.0f}%")

    # ---------- F2 去集中度
    print("\n=== F2 剔除 TOP5 贡献品种后（OOS）===")
    dfa = df[~df.symbol.isin(TOP5)]
    t_a = V2.backtest_family(dfa, "mr", float(bis.thr), float(bis.exit), float(bis.stop))
    oos_a = t_a[t_a.period == "OOS"]
    is_a = t_a[t_a.period == "IS"]
    print(f"  去TOP5后 IS {is_a.net_pct.sum():+.1f}% (n={len(is_a)}) / "
          f"OOS {oos_a.net_pct.sum():+.1f}% (n={len(oos_a)}, "
          f"PF={oos_a.loc[oos_a.net_pct>0,'net_pct'].sum()/max(-oos_a.loc[oos_a.net_pct<0,'net_pct'].sum(),1e-9):.2f})")

    # 对照：全品种同参数
    t_all = cache[(float(bis.thr), float(bis.exit), float(bis.stop))]
    oos_all = t_all[t_all.period == "OOS"]
    print(f"  全品种对照 IS {t_all[t_all.period=='IS'].net_pct.sum():+.1f}% / "
          f"OOS {oos_all.net_pct.sum():+.1f}% (n={len(oos_all)})")
    print(f"  => 集中度依赖度：OOS 总收益 去TOP5后保留 "
          f"{100*oos_a.net_pct.sum()/max(oos_all.net_pct.sum(),1e-9):.0f}%")

    # ---------- F5 基准对照：随机入场（同参数、同出场规则）
    print("\n=== F5 基准对照：随机入场（同参数）===")
    rng = np.random.default_rng(2026)
    rnd_tot = []
    for k in range(20):
        d2 = df.copy()
        # 随机入场 = 随机挑选日期触发（用伪随机序列替代价格突破判定）
        d2["_r"] = rng.random(len(d2))
        tot_oos, tot_is = 0.0, 0.0
        for sym, g in d2.groupby("symbol"):
            g = g.reset_index(drop=True)
            c = g.close.values
            i = 1
            while i < len(g) - 2:
                if g._r.values[i] < 0.02:      # 2% 概率随机触发
                    dirn = 1 if rng.random() < 0.5 else -1
                    entry = c[i]
                    tgt = entry * (1 + dirn * float(bis.exit))
                    stp = entry * (1 - dirn * float(bis.stop))
                    j, ex_i, rsn, px = i + 1, None, None, None
                    while j < len(g):
                        hi, lo = g.high.values[j], g.low.values[j]
                        if dirn > 0:
                            if lo <= stp: ex_i, rsn, px = j, "stop", stp; break
                            if hi >= tgt: ex_i, rsn, px = j, "target", tgt; break
                        else:
                            if hi >= stp: ex_i, rsn, px = j, "stop", stp; break
                            if lo <= tgt: ex_i, rsn, px = j, "target", tgt; break
                        j += 1
                    if ex_i is None:
                        ex_i = len(g) - 1; rsn = "eod"; px = float(g.close.values[ex_i])
                    net = dirn * (px - entry) / entry * 100.0 - 2 * V2.COST_BP
                    if pd.Timestamp(g.d.values[ex_i]) <= IS_END:
                        tot_is += net
                    else:
                        tot_oos += net
                    i = ex_i + 1
                else:
                    i += 1
        rnd_tot.append((tot_is, tot_oos))
    R = np.array(rnd_tot)
    print(f"  20 次随机入场: IS 中位 {np.median(R[:,0]):+.1f}% (范围 {R[:,0].min():+.0f}~{R[:,0].max():+.0f}) / "
          f"OOS 中位 {np.median(R[:,1]):+.1f}% (范围 {R[:,1].min():+.0f}~{R[:,1].max():+.0f})")
    print(f"  mr 策略 OOS {oos_all.net_pct.sum():+.1f}%  →  "
          f"超过全部 {20} 次随机: {100*(R[:,1] < oos_all.net_pct.sum()).mean():.0f}%")

    # ---------- F1 半样本
    print("\n=== F1 半样本：前半(2015-2019)选参 -> 后半(2020-2025)验 ===")
    d1 = df[df.d <= pd.Timestamp("2019-12-31")]
    d2 = df[df.d >= pd.Timestamp("2020-01-01")]
    b1 = None
    rows1 = []
    for thr in [0.04, 0.06, 0.08, 0.10, 0.12]:
        for ex in [0.03, 0.05, 0.08, 0.10, 0.12]:
            for st in [0.05, 0.10, 0.15]:
                t = V2.backtest_family(d1, "mr", thr, ex, st)
                if len(t) == 0:
                    continue
                rows1.append(dict(thr=thr, exit=ex, stop=st, tot=t.net_pct.sum()))
    H1 = pd.DataFrame(rows1).sort_values("tot", ascending=False)
    print("  前半 TOP3:"); print(H1.head(3).to_string(index=False))
    b1 = H1.iloc[0]
    t2 = V2.backtest_family(d2, "mr", float(b1.thr), float(b1.exit), float(b1.stop))
    print(f"  前半最优 thr={b1.thr} ex={b1.exit} st={b1.stop} ({b1.tot:+.1f}%) "
          f"-> 后半 {t2.net_pct.sum():+.1f}% (n={len(t2)}) "
          f"同号={'是' if np.sign(b1.tot)==np.sign(t2.net_pct.sum()) else '否'}")

    print("\n[done] ->", OUT)


if __name__ == "__main__":
    main()
