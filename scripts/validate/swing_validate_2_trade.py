"""
Step 4-6: 判决交易可行性 —— "幅度统计"能不能变成正期望 + 可控止损
=====================================================================
前面已测：
  Q1 波段幅度高度稳定（跨品种 log-sd 0.10，跨 11 年几乎不变）-> 可当"尺度基准"
  Q2 幅度序列自相关 ≈ 0                                  -> 幅度不可外推
  Q3 趋势状态条件化差异 p<0.001 但幅度差仅 1.4pp vs CV 0.52 -> 实务上无区分力

本步回答：用户真正想做的事（"低买高卖"）能否成立？
  T1 幅度本身能否预测【方向】？（涨后是否倾向于继续涨/回撤）
  T2 三类规则族回测（成本后、OOS、参数曲面）：
     R-A  纯均值回归：涨 N% 后做空、跌 N% 后做多（用户"低买高卖"直译）
     R-B  趋势跟踪：突破/顺势 + 幅度止损
     R-C  状态门控（用户隐含方案）：趋势中回调买、震荡中反向
  T3 参数曲面（防过拟合核心）：阈值扫全网格，看是否有"平台"而非"尖峰"
  T4 逐年同号 + 半年拆分 + bootstrap p 值

成本：国内期货单边手续费+滑点，按 2bp 单边（双边 4bp）计，另测 5bp/10bp 压力
纪律：IS=2015-2021，OOS=2022-2025；所有参数只在 IS 选，OOS 只验一次
"""
import os
import numpy as np
import pandas as pd
import psycopg2

PG = dict(host="127.0.0.1", port=5432, user="futures",
          password=os.environ["POSTGRES_PASSWORD"], dbname="futures")
OUT = r"E:\Docker\qhyc\docs\_swing_out"
os.makedirs(OUT, exist_ok=True)

IS_END = pd.Timestamp("2021-12-31")
COST_BP = 2.0        # 单边成本 bp（手续费+滑点）
MULT = 1.0           # 合约乘数：日线点数 -> 盈亏金额。默认 1（用收益率口径，不依赖乘数）


def load():
    sql = """
        SELECT symbol, trade_datetime::date AS d,
               high::float8, low::float8, close::float8
        FROM fut_kline
        WHERE freq='daily' AND kind='continuous'
          AND trade_datetime >= '2015-01-01' AND trade_datetime < '2025-12-31'
        ORDER BY symbol, trade_datetime
    """
    with psycopg2.connect(**PG) as cn:
        df = pd.read_sql(sql, cn, parse_dates=["d"])
    bad = df.low <= 0
    df.loc[bad, "low"] = df.loc[bad, ["close"]].min(axis=1)
    df = df[df.low > 0].reset_index(drop=True)
    df["ret"] = df.groupby("symbol").close.pct_change()
    df["ma20"] = df.groupby("symbol").close.transform(lambda s: s.rolling(20).mean())
    return df


# ------------------------------------------------------------------ T1
def t1_direction(L):
    """
    幅度能否预测未来。
    注意：ZigZag 腿天然交替（涨腿后必跟跌腿），故 P(下一腿同向) 恒为 0，
    该指标无意义（第一版跑出 0.000/0.997 就是这个陷阱）。
    有效测法两种：
      A) spearman(当前幅度, 下一腿幅度) —— 幅度是否自相关
      B) 条件于【趋势结构】：用前 3 腿的净位移判定"趋势/震荡"，
         再看下一腿幅度与方向 —— 这才是用户"上涨趋势中回调 40-50 点"的对应量。
    """
    print("\n=== T1 幅度 -> 未来 的预测力 ===")
    rows = []
    for thr, g in L.groupby("thr"):
        for sym, gg in g.sort_values(["sym", "confirm_i"]).groupby("sym"):
            gg = gg.reset_index(drop=True)
            if len(gg) < 8:
                continue
            a = gg.amp_pct.values
            up = gg.up.values.astype(bool)
            for i in range(3, len(gg)):
                # 前 3 腿净位移（用 confirm_i 之后的信息 -> 严格因果）
                net3 = np.sum(np.where(up[i - 3:i], a[i - 3:i], -a[i - 3:i]))
                rows.append(dict(thr=thr, sym=sym, amp=a[i], up_i=up[i],
                                 next_up=up[i + 1] if i + 1 < len(gg) else np.nan,
                                 next_amp=a[i + 1] if i + 1 < len(gg) else np.nan,
                                 net3=net3, yr=gg.yr.values[i]))
    D = pd.DataFrame(rows).dropna(subset=["next_amp"])
    if len(D) == 0:
        print("  样本不足")
        return D
    from scipy.stats import spearmanr
    print("--- A) 幅度自相关：spearman(当前幅度, 下一腿幅度) ---")
    for thr, g in D.groupby("thr"):
        r, p = spearmanr(g.amp, g.next_amp)
        print(f"  thr={thr}: rho={r:+.3f} (p={p:.3f}, n={len(g)})  "
              f"→ 幅度是否可外推")

    print("--- B) 条件于趋势结构（净位移>0 为上涨趋势）的下一腿 ---")
    for thr, g in D.groupby("thr"):
        g = g.copy()
        g["struc"] = np.where(g.net3 > 0, "上涨趋势", "下跌趋势")
        for st, gg in g.groupby("struc"):
            amp_med = gg.amp.median()
            hi = gg[gg.amp >= amp_med]
            print(f"  thr={thr} {st}: n={len(gg):5d} | 当前腿中位幅度={amp_med:5.1f}% | "
                  f"大幅腿后下一腿中位={gg.next_amp.median():5.1f}% | "
                  f"大幅组下一腿中位={hi.next_amp.median():5.1f}% "
                  f"比值(大幅/全体)={hi.next_amp.median()/gg.next_amp.median():.3f}")
    D.to_csv(os.path.join(OUT, "t1_direction.csv"), index=False)
    return D


# ------------------------------------------------------------------ backtest
def backtest_family(df, family, thr_pct, exit_pct, stop_pct, gate=None):
    """
    通用事件回测。逐 bar、无未来函数：信号在收盘产生，次日开盘进场近似用次收盘。
    简化：以 ATR 无关的百分比阈值触发，持仓到 (entry * (1±exit)) 或 (entry * (1∓stop))
    family:
      'mr'  均值回归：跌 thr 做多 / 涨 thr 做空（反向）
      'tf'  趋势跟踪：涨 thr 做多 / 跌 thr 做空
      'gate' 状态门控：多头时(price>ma20) 涨 thr 做空、跌 thr 做多；空头反之
    """
    trades = []
    for sym, g in df.groupby("symbol"):
        g = g.reset_index(drop=True)
        c = g.close.values
        ma = g.ma20.values
        d = g.d.values
        i = 1
        while i < len(g) - 2:
            p0 = c[i - 1]
            if not np.isfinite(p0) or p0 <= 0:
                i += 1
                continue
            mv = p0 * thr_pct
            dirn = 0
            if family == "mr":
                if c[i] - p0 > mv:
                    dirn = -1
                elif p0 - c[i] > mv:
                    dirn = 1
            elif family == "tf":
                if c[i] - p0 > mv:
                    dirn = 1
                elif p0 - c[i] > mv:
                    dirn = -1
            elif family == "gate":
                m = ma[i]
                if not np.isfinite(m):
                    i += 1
                    continue
                if c[i] > m:            # 多头状态
                    if c[i] - p0 > mv:
                        dirn = -1        # 涨多了做空
                    elif p0 - c[i] > mv:
                        dirn = 1         # 回调做多
                else:
                    if p0 - c[i] > mv:
                        dirn = 1
                    elif c[i] - p0 > mv:
                        dirn = -1
            if dirn == 0:
                i += 1
                continue
            entry = c[i]
            tgt = entry * (1 + dirn * exit_pct)
            stp = entry * (1 - dirn * stop_pct)
            j = i + 1
            exit_i, reason, px = None, None, None
            while j < len(g):
                hi, lo = g.high.values[j], g.low.values[j]
                if dirn > 0:
                    if lo <= stp:
                        exit_i, reason, px = j, "stop", stp
                        break
                    if hi >= tgt:
                        exit_i, reason, px = j, "target", tgt
                        break
                else:
                    if hi >= stp:
                        exit_i, reason, px = j, "stop", stp
                        break
                    if lo <= tgt:
                        exit_i, reason, px = j, "target", tgt
                        break
                j += 1
            if exit_i is None:
                exit_i = len(g) - 1
                reason = "eod"
                px = float(g.close.values[exit_i])
            gross = dirn * (px - entry) / entry * 100.0        # 百分比收益
            net = gross - 2 * COST_BP / 100.0                 # 双边成本，百分比口径
            trades.append(dict(sym=sym, entry_d=pd.Timestamp(d[i]),
                               exit_d=pd.Timestamp(d[exit_i]),
                               dirn=int(dirn), gross_pct=float(gross), net_pct=float(net),
                               reason=reason, bars=int(exit_i - i),
                               thr=thr_pct, exit=exit_pct, stop=stop_pct,
                               family=family,
                               period="IS" if pd.Timestamp(d[i]) <= IS_END else "OOS"))
            i = exit_i + 1
    T = pd.DataFrame(trades)
    if len(T):
        T["net_pct"] = T["net_pct"].astype(float)
        T["gross_pct"] = T["gross_pct"].astype(float)
    return T


def stats(t, label):
    if len(t) == 0:
        return dict(label=label, n=0)
    t = t.copy()
    n = len(t)
    tot = t.net_pct.sum()
    sharpe = t.net_pct.mean() / t.net_pct.std(ddof=1) * np.sqrt(252 * 3) if t.net_pct.std() > 0 else 0
    pf_pos = t.loc[t.net_pct > 0, "net_pct"].sum()
    pf_neg = -t.loc[t.net_pct < 0, "net_pct"].sum()
    dd = []
    for sym, g in t.sort_values("exit_d").groupby("sym"):
        eq = g.net_pct.cumsum()
        dd.append((eq - eq.cummax()).min())
    return dict(label=label, n=n, total=tot, per_trade=t.net_pct.mean() * n,
                win=(t.net_pct > 0).mean(), pf=pf_pos / pf_neg if pf_neg > 0 else np.nan,
                sharpe=sharpe, mdd=min(dd) if dd else np.nan,
                stop_rate=(t.reason == "stop").mean())


# ------------------------------------------------------------------ T2/T3
def main():
    df = load()
    print(f"[data] rows={len(df)} syms={df.symbol.nunique()} {df.d.min().date()}~{df.d.max().date()}")
    L = pd.read_csv(os.path.join(OUT, "legs.csv"))
    t1_direction(L)

    print("\n=== T2 三类规则族回测（成本后 %/笔，IS=2015-2021 / OOS=2022-2025）===")
    grid_thr = [0.04, 0.06, 0.08, 0.12]
    grid_exit = [0.04, 0.08, 0.12]
    grid_stop = [0.03, 0.06, 0.10]
    all_rows = []
    surf = {}
    for fam in ["mr", "tf", "gate"]:
        for thr in grid_thr:
            for ex in grid_exit:
                for st in grid_stop:
                    t = backtest_family(df, fam, thr, ex, st)
                    if len(t) == 0:
                        continue
                    s_is = stats(t[t.period == "IS"], f"{fam} {thr}/{ex}/{st} IS")
                    s_oos = stats(t[t.period == "OOS"], f"{fam} {thr}/{ex}/{st} OOS")
                    s_all = stats(t, f"{fam} {thr}/{ex}/{st} ALL")
                    for s in (s_is, s_oos, s_all):
                        s.update(dict(family=fam, thr=thr, exit=ex, stop=st))
                        all_rows.append(s)
                    surf[(fam, thr, ex, st)] = dict(
                        is_total=s_is["total"], oos_total=s_oos["total"],
                        all_total=s_all["total"], all_n=s_all["n"],
                        all_pf=s_all["pf"], all_sharpe=s_all["sharpe"],
                        all_mdd=s_all["mdd"], all_win=s_all["win"])
    R = pd.DataFrame(all_rows)
    R.to_csv(os.path.join(OUT, "t2_family_grid.csv"), index=False)

    for fam in ["mr", "tf", "gate"]:
        sub = R[R.family == fam]
        a = sub[sub.label.str.endswith("ALL")].copy()
        if len(a) == 0:
            continue
        a = a.sort_values("total", ascending=False)
        print(f"\n--- {fam}: 全部 {len(a)} 组参数，总收益 TOP5 / BOTTOM3 ---")
        cols = ["label", "n", "total", "win", "pf", "sharpe", "mdd"]
        print(a[cols].head(5).to_string(index=False))
        print(a[cols].tail(3).to_string(index=False))
        pos = (a.total > 0).sum()
        print(f"  正收益参数组: {pos}/{len(a)} = {pos/len(a)*100:.0f}%")
        # IS 选最优 -> OOS 表现
        best_is = sub[sub.label.str.endswith("IS")].sort_values("total", ascending=False).iloc[0]
        thr_s = best_is.thr; ex_s = best_is.exit; st_s = best_is.stop
        oos = sub[(sub.label.str.endswith("OOS")) & (sub.thr == thr_s) &
                  (sub.exit == ex_s) & (sub.stop == st_s)].iloc[0]
        print(f"  ** IS 最优参数 thr={thr_s} exit={ex_s} stop={st_s} -> IS总={best_is.total:.1f}%"
              f"  OOS总={oos.total:.1f}% (n={oos.n}, PF={oos.pf:.2f})")

    # T3 参数曲面稳健性：每个族的正收益比例 + 邻域一致性
    print("\n=== T3 参数曲面稳健性（防过拟合）===")
    for fam in ["mr", "tf", "gate"]:
        s = surf
        rows = [(k, v) for k, v in s.items() if k[0] == fam]
        if not rows:
            continue
        tot = np.array([v["all_total"] for _, v in rows])
        oos = np.array([v["oos_total"] for _, v in rows])
        print(f"{fam}: 组数={len(tot)} 全样本正={100*(tot>0).mean():.0f}% "
              f"OOS正={100*(oos>0).mean():.0f}% "
              f"IS-OOS同号={100*(np.sign(tot)==np.sign(oos)).mean():.0f}% "
              f"参数敏感度(P90-P10)/|P50|={(np.percentile(tot,90)-np.percentile(tot,10))/max(abs(np.median(tot)),1e-9):.1f}x")

    # T4 最优族的逐年 + bootstrap
    print("\n=== T4 逐年稳定性 + bootstrap（取每个族 IS 最优参数）===")
    rng = np.random.default_rng(7)
    for fam in ["mr", "tf", "gate"]:
        sub = R[R.family == fam]
        bis = sub[sub.label.str.endswith("IS")].sort_values("total", ascending=False).iloc[0]
        t = backtest_family(df, fam, bis.thr, bis.exit, bis.stop)
        if len(t) == 0:
            continue
        yr = t.groupby(pd.DatetimeIndex(t.exit_d).year).net_pct.sum()
        print(f"\n{fam} (thr={bis.thr} ex={bis.exit} st={bis.stop}) 逐年净收益%:")
        print("  " + "  ".join(f"{k}:{v:+.1f}" for k, v in yr.items()))
        pos_yr = int((yr > 0).sum())
        # bootstrap on trade level
        bs = []
        arr = t.net_pct.values
        for _ in range(2000):
            bs.append(rng.choice(arr, size=len(arr), replace=True).sum())
        bs = np.array(bs)
        print(f"  同号年份 {pos_yr}/{len(yr)}；bootstrap P(总收益<=0)={float((bs<=0).mean()):.3f}"
              f"；中位总收益={np.median(bs):+.1f}%")
        t.to_csv(os.path.join(OUT, f"trades_{fam}.csv"), index=False)

    # 成本压力
    print("\n=== 成本压力测试（各族 IS 最优参数，COST 单边 2/5/10 bp）===")
    global COST_BP
    base = []
    for fam in ["mr", "tf", "gate"]:
        sub = R[R.family == fam]
        bis = sub[sub.label.str.endswith("IS")].sort_values("total", ascending=False).iloc[0]
        base.append((fam, bis.thr, bis.exit, bis.stop))
    for cost in [2.0, 5.0, 10.0]:
        COST_BP = cost
        line = [f"成本{cost:.0f}bp:"]
        for fam, thr, ex, st in base:
            t = backtest_family(df, fam, thr, ex, st)
            a = stats(t, fam)
            line.append(f"{fam}总={a['total']:+.1f}% PF={a['pf']:.2f}")
        print("  " + " | ".join(line))
    COST_BP = 2.0

    print("\n[done] ->", OUT)


if __name__ == "__main__":
    main()
