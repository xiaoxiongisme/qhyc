"""
裸趋势跟随基线实验：市场是否存在可捕捉的 alpha？
================================================
【目的】前三轮验证收敛到根因"V3.4 入场信号不承载 alpha"。但在下结论"策略设计无用"之前，
必须先回答一个更基础的问题：**这个市场本身有没有可捕捉的趋势？**

【若裸趋势也无 alpha】→ 问题在市场/成本结构，V3.4 怎么改都没用，整个项目定位要重估。
【若裸趋势有 alpha】     → 说明 V3.4 的门控（ADX/斐波/加码）在破坏它，才有救。

【设计纪律：参数克制，不扫网格】
  双均线：只测 3 组有先验依据的经典组合（10/30、20/60、50/200），不做参数搜索
  唐奇安：只测 1 组经典 55 日通道
  止损：统一 2×ATR（与 V3.4 相同，便于对比）
  全部只用趋势跟随这一种逻辑，不叠加任何 V3.4 门控

【必做对照】随机入场（相同出场/止损规则）
  若随机入场表现与趋势策略相当 → 说明没有 alpha，只是在赌波动
  这条对照是本实验的核心，缺了它结论无效

【口径】与前三轮完全一致：状态右移 2 根 / perlot 计价 / 每品种 SPEC 真实成本 / 容量 5 FIFO
       IS=2015-2021 / OOS=2022-2025
"""
import os
import sys
import numpy as np
import pandas as pd
import pickle

sys.path.insert(0, r"E:\Docker\qhyc")
OUT = r"E:\Docker\qhyc\docs\_swing_out"

import psycopg2
from app.core.config import get_settings
from app.strategies.fusion_signal import ema, atr14

_MC = get_settings().main_contracts
MULT = {m.symbol: float(m.multiplier) for m in _MC}
SPEC = {
 "FG":(1,6),"SA":(1,3.5),"SR":(1,3),"CF":(5,4.3),"TA":(2,3),"MA":(1,2),"RM":(1,1.5),
 "OI":(1,2),"AP":(1,5),"UR":(1,5),"SH":(1,4),"PX":(2,3),"SF":(2,3),"SM":(2,3),
 "RB":(1,4),"HC":(1,4),"SS":(5,4),"BU":(1,4),"RU":(5,5),"SP":(2,5),"AO":(1,6),
 "CU":(10,17),"AL":(5,3),"ZN":(5,3),"PB":(5,3),"NI":(10,3),"SN":(10,3),"AU":(0.02,10),
 "AG":(1,5),"FU":(1,2),"SC":(0.1,20),"SI":(5,6),"LC":(20,8),
 "A":(1,2),"B":(1,1),"M":(1,1.5),"Y":(2,2.5),"P":(2,3),"C":(1,1.2),"CS":(1,1.5),
 "JD":(1,8),"L":(1,1),"V":(1,1),"PP":(1,3),"J":(0.5,15),"JM":(0.5,15),"I":(0.5,10),
 "EG":(1,4),"EB":(1,3),"LH":(5,20),
}
_PROD = {m.symbol: m.product.upper() for m in _MC}
IS_SPLIT = pd.Timestamp("2023-12-31", tz="UTC")
HALF = pd.Timestamp("2020-12-31", tz="UTC")
SL_ATR = 2.0          # 与 V3.4 一致
PG = dict(host="127.0.0.1", port=5432, user="futures",
          password="qhyc_dev_pwd_2026", dbname="futures")


def cost(sym, mult, lots=1):
    t, f = SPEC[_PROD[sym]]
    return (2 * f + 2 * 1.0 * (t * mult)) * lots


def load_daily():
    p = os.path.join(OUT, "daily_bars.pkl")
    if os.path.exists(p):
        with open(p, "rb") as f:
            return pickle.load(f)
    bars = {}
    for m in _MC:
        for cand in (m.product.upper(), m.product.lower()):
            sql = ("SELECT trade_datetime, open::float8, high::float8, low::float8, close::float8 "
                   "FROM fut_kline WHERE freq='daily' AND kind='continuous' "
                   f"AND symbol='KQ.m@{m.exchange}.{cand}' AND trade_datetime>='2015-01-01' "
                   "ORDER BY trade_datetime ASC")
            with psycopg2.connect(**PG) as cn:
                df = pd.read_sql(sql, cn, parse_dates=["trade_datetime"])
            if len(df):
                bars[m.symbol] = dict(dt=df.trade_datetime.to_numpy(),
                                       o=df.open.to_numpy(float), h=df.high.to_numpy(float),
                                       l=df.low.to_numpy(float), c=df.close.to_numpy(float))
                break
    with open(p, "wb") as f:
        pickle.dump(bars, f)
    return bars


def _positions_from_signal(bars, signals):
    """signals: {sym: array of 0/1/-1}（1=多 -1=空 0=无仓）→ 头寸 + 右移 2 根对齐。"""
    out = []
    for sym, b in bars.items():
        sig = signals.get(sym)
        if sig is None:
            continue
        o, h, l, c, dt = b["o"], b["h"], b["l"], b["c"], b["dt"]
        n = len(c)
        if n < 60:
            continue
        atr = atr14(h, l, c, 14)
        st = np.zeros(n, dtype=int)
        st[:] = 0
        for i in range(n):
            if np.isfinite(sig[i]) and sig[i] != 0:
                st[i] = int(sig[i])
        st[:2] = 0          # 右移 2 根（与 V3.4 一致）
        cur = None
        for i in range(n):
            s = int(st[i])
            if cur is None:
                if s != 0 and np.isfinite(atr[i]):
                    cur = dict(dir=s, first_px=float(c[i]), entry_i=i, entry_dt=dt[i],
                               e_atr=float(atr[i]), lot_pxs=[float(c[i])])
            else:
                if s == cur["dir"]:
                    pass
                else:
                    # 离场：收盘价判断（不用未来 bar）
                    cur["exit_px"] = float(c[i]); cur["exit_i"] = i; cur["exit_dt"] = dt[i]
                    cur["sym"] = sym
                    out.append(cur); cur = None
                    if s != 0 and np.isfinite(atr[i]):
                        cur = dict(dir=s, first_px=float(c[i]), entry_i=i, entry_dt=dt[i],
                                   e_atr=float(atr[i]), lot_pxs=[float(c[i])])
        if cur is not None:
            cur["exit_px"] = float(c[-1]); cur["exit_i"] = n - 1; cur["exit_dt"] = dt[-1]
            cur["sym"] = sym; cur["open_at_end"] = True
            out.append(cur)
    return out


def sig_dual_ma(b, fast, slow):
    """双均线趋势跟踪：快线上穿慢线做多，下穿做空（或空仓）；只做顺势一侧。"""
    c = b["c"]
    ef = ema(c, fast); es = ema(c, slow)
    n = len(c)
    sig = np.zeros(n)
    pos = 0
    for i in range(n):
        if not (np.isfinite(ef[i]) and np.isfinite(es[i])):
            sig[i] = pos
            continue
        if ef[i] > es[i] and pos <= 0:
            pos = 1
        elif ef[i] < es[i] and pos >= 0:
            pos = -1
        sig[i] = pos
    return sig


def sig_donchian(b, win=55):
    """唐奇安通道突破：收盘创 win 根新高做多、创 win 根新低做空；反向则平仓。"""
    c = b["c"]; n = len(c)
    sig = np.zeros(n); pos = 0
    for i in range(n):
        if i < win:
            sig[i] = pos
            continue
        hh = np.max(c[i - win:i]); ll = np.min(c[i - win:i])
        if c[i] > hh and pos <= 0:
            pos = 1
        elif c[i] < ll and pos >= 0:
            pos = -1
        sig[i] = pos
    return sig


def sig_random(b, seed):
    """随机入场对照：方向随机、离场随机（用于检验是否存在 alpha）。"""
    n = len(b["c"])
    rng = np.random.default_rng(seed)
    sig = np.zeros(n); pos = 0
    for i in range(n):
        if rng.random() < 0.02:          # 每日 2% 概率换向
            pos = 1 if rng.random() < 0.5 else -1
        sig[i] = pos
    return sig


def replay(pos_list, cap=5):
    tl = []
    for p in pos_list:
        tl.append((p["entry_dt"], 0, p))
        if not p.get("open_at_end"):
            tl.append((p["exit_dt"], 1, p))
    tl.sort(key=lambda x: (x[0], x[1]))
    openp = {}
    eq = peak = mdd = 0.0
    w = l = ex = 0
    rows = []
    for dt, typ, p in tl:
        sym = p["sym"]
        if typ == 1:
            if sym in openp:
                pp = openp.pop(sym)
                eq += pp["Y"]; w += int(pp["win"]); l += int(not pp["win"])
                peak = max(peak, eq); mdd = max(mdd, peak - eq)
                rows.append(pp)
        else:
            if p.get("open_at_end"):
                continue
            if len(openp) >= cap or sym in openp:
                continue
            mult = MULT[sym]; d = 1.0 if p["dir"] == 1 else -1.0
            lots = len(p["lot_pxs"])
            gross = sum(d * (p["exit_px"] - px) * mult for px in p["lot_pxs"])
            Y = gross - cost(sym, mult, lots)
            openp[sym] = dict(Y=Y, win=Y > 0, p=p, gross=gross, cost=cost(sym, mult, lots))
            ex += 1
    n = w + l
    return dict(executed=ex, win=w / n if n else 0.0, net=eq, mdd=mdd,
                mar=eq / mdd if mdd > 0 else 0.0, rows=rows)


def summarize(tag, r, gates=True):
    L = pd.DataFrame([dict(sym=x["p"]["sym"], dt=pd.Timestamp(x["p"]["entry_dt"]), Y=x["Y"],
                           gross=x["gross"], cost=x["cost"]) for x in r["rows"]])
    if not len(L):
        print(f"  {tag}: 无交易")
        return None
    isv = L[L.dt <= IS_SPLIT].Y.sum(); oosv = L[L.dt > IS_SPLIT].Y.sum()
    e = L[L.dt <= HALF].Y.sum(); l2 = L[L.dt > HALF].Y.sum()
    oosL = L[L.dt > IS_SPLIT]
    t5 = oosL.groupby("sym").Y.sum().nlargest(5).index.tolist() if len(oosL) else []
    no5 = oosL[~oosL.sym.isin(t5)].Y.sum() if len(oosL) else 0.0
    ratio = 100 * L.cost.sum() / max(L.gross.sum(), 1e-9)
    print(f"  {tag}")
    print(f"    n={r['executed']:5d} 净{r['net']/1e4:+8.1f}万 胜{r['win']*100:5.1f}% "
          f"DD{r['mdd']/1e4:6.1f}万 MAR{r['mar']:+6.2f} | 毛利{L.gross.sum()/1e4:+8.1f}万 "
          f"成本{L.cost.sum()/1e4:6.1f}万 比{ratio:5.0f}%")
    if gates:
        yr = L.groupby(L.dt.dt.year).Y.sum()
        print(f"    IS{isv/1e4:+7.1f}万 OOS{oosv/1e4:+7.1f}万 {'✅同号' if np.sign(isv)==np.sign(oosv) else '❌翻号'}"
              f" | 半样本 前{e/1e4:+6.1f}/后{l2/1e4:+6.1f} | 去TOP5后OOS{no5/1e4:+7.1f}万")
        print("    逐年 " + " ".join(f"{k}:{v/1e4:+.0f}" for k, v in yr.items()))
    return dict(tag=tag, net=r["net"], n=r["executed"], isv=isv, oosv=oosv,
                same=int(np.sign(isv) == np.sign(oosv)), oos_no5=no5, ratio=ratio,
                gross=L.gross.sum())


def main():
    bars = load_daily()
    print(f"[data] 日线 {len(bars)} 品种 | 止损统一 2×ATR（与 V3.4 一致）| "
          f"容量 5 FIFO | 真实成本 SPEC | 状态右移 2 根")
    print("[设计] 参数克制：3 组经典双均线 + 1 组唐奇安，不扫网格")

    print("\n" + "=" * 88)
    print("STEP 1  裸趋势跟随（双均线 3 组 + 唐奇安 1 组）")
    print("=" * 88)
    res = []
    for fast, slow in [(10, 30), (20, 60), (50, 200)]:
        sig = {s: sig_dual_ma(b, fast, slow) for s, b in bars.items()}
        pos = _positions_from_signal(bars, sig)
        r = replay(pos)
        g = summarize(f"双均线 {fast}/{slow}", r)
        if g:
            res.append(g)
    sig = {s: sig_donchian(b, 55) for s, b in bars.items()}
    pos = _positions_from_signal(bars, sig)
    r = replay(pos)
    g = summarize("唐奇安 55日突破", r)
    if g:
        res.append(g)

    print("\n" + "=" * 88)
    print("STEP 2  ★ 随机入场对照（相同出场规则/成本/容量）——本实验的核心")
    print("=" * 88)
    print("  若随机 ≈ 趋势策略 → 无 alpha，趋势规则没有贡献")
    rnd = []
    for seed in range(10):
        sig = {s: sig_random(b, seed) for s, b in bars.items()}
        pos = _positions_from_signal(bars, sig)
        r = replay(pos)
        L = pd.DataFrame([dict(sym=x["p"]["sym"], dt=pd.Timestamp(x["p"]["entry_dt"]), Y=x["Y"],
                               gross=x["gross"], cost=x["cost"]) for x in r["rows"]])
        if not len(L):
            continue
        rnd.append(dict(seed=seed, net=r["net"], n=r["executed"],
                        isv=L[L.dt <= IS_SPLIT].Y.sum(), oosv=L[L.dt > IS_SPLIT].Y.sum(),
                        gross=L.gross.sum()))
    for x in rnd[:3]:
        print(f"  随机 seed={x['seed']}: n={x['n']:5d} 净{x['net']/1e4:+8.1f}万 "
              f"毛利{x['gross']/1e4:+7.1f}万 | IS{x['isv']/1e4:+7.1f} OOS{x['oosv']/1e4:+7.1f}")
    nets = np.array([x["net"] for x in rnd])
    print(f"\n  10 次随机入场: 净利 中位{np.median(nets)/1e4:+.1f}万 "
          f"范围{nets.min()/1e4:+.1f}~{nets.max()/1e4:+.1f}万")
    best_trend = max(x["net"] for x in res)
    beat = int((nets > best_trend).sum())
    print(f"  最好的裸趋势策略 {best_trend/1e4:+.1f}万 > 10 次随机中的 {10-beat} 次 "
          f"(即随机有 {beat}/10 次跑赢最好趋势策略)")

    print("\n" + "=" * 88)
    print("STEP 3  判决")
    print("=" * 88)
    print(f"  {'策略':<20}{'成交':>7}{'净利(万)':>11}{'毛利(万)':>11}{'成本/毛利':>10}"
          f"{'IS(万)':>10}{'OOS(万)':>10}{'同号':>7}")
    for x in res:
        print(f"  {x['tag'][:18]:<20}{x['n']:>7}{x['net']/1e4:>11.1f}{x['gross']/1e4:>11.1f}"
              f"{x['ratio']:>9.0f}%{x['isv']/1e4:>10.1f}{x['oosv']/1e4:>10.1f}"
              f"{'  ✅' if x['same'] else '  ❌':>7}")
    print(f"  {'随机入场(中位)':<20}{'':>7}{np.median(nets)/1e4:>11.1f}{'':>11}{'':>10}{'':>10}{'':>10}{'':>7}")
    print()
    pos_trends = [x for x in res if x["net"] > 0]
    if pos_trends and np.median(nets) < 0:
        print("  ✅ 存在正期望的裸趋势策略，且随机入场中位为负 → **市场有可捕捉的趋势 alpha**")
        print("     ⇒ V3.4 的问题在门控/加码设计（它们破坏了趋势），不在市场")
    elif np.median(nets) > 0:
        print("  ❌ 随机入场均能盈利 → 边际来自波动而非方向，趋势规则未贡献 alpha")
        print("     ⇒ 问题在市场/成本结构，V3.4 与任何方向策略都难盈利")
    else:
        print("  ⚠ 裸趋势全负且随机为负 → 该样本区间内趋势不可捕捉（可能与 V3.4 同因）")


if __name__ == "__main__":
    main()
