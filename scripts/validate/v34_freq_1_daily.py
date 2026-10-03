"""
V3.4 决策周期验证：小时线 vs 日线
=================================
【要验的假设】前两轮结论=根因是"信号时效性"：V3.4 小时线信号的信息量不足以区分未来 3 天走势。
  证据链：①加码单胜率随持仓单调升（6-24h 11% → 3-7天 57% → >14天 94%）
          ②单手单任何时长都亏（胜率 0.0-2.6%，笔均毛利与时长无关）⇒ 入场即注定
          ③入场时三特征(ADX/相对ATR/EMA20偏离)对"是否成为加码单" spearman≈0.01
          ④所有参数/集中度路线全败

【若假设成立，日线应该更好】因为赢家需要 3-7 天兑现，而小时线信号在这个尺度上噪声太大。

【口径来源（不自己缩放参数）】
  官方日线实现 imports/SX/_fut_daily_brief.py 明确：
    方向层 = 日线 EMA20（≈ 小时 EMA140）
    入场层 = 日线 EMA10（≈ 小时 EMA20）
    门槛与周期无关：ADX_GATE=15 / FIB_W=60 / FIB_TOL_ATR=0.5 / FIB_RATIOS=(0.382,0.5,0.618)
                    SLK=2.0 / TRK=2.0 / BER=0.5
  ⇒ 这就是"同一策略换周期"，不是"换了个策略"。

【关键公平性】
  小时线 ema_k=140 ≈ 58 根日线（每天约 6 根小时线：日盘4+夜盘2）——但官方口径给的是 EMA20，
  说明官方认为 140 根小时线 ≈ 20 根日线（即 1 根小时线 ≈ 0.14 根日线 → 20×6=120 根小时线/20日线
  ≈ 每天 6 根）。本脚本按官方口径执行，同时做一组"等比缩放"对照以排除口径争议。

【防未来函数】
  复用 walk_fusion_states（逐 bar walk-forward），日线同样右移 2 根对齐。
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
from app.backtest.fusion_backtest import FusionBacktestParams, _htf_direction
from app.strategies.fusion_signal import walk_fusion_states

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
PG = dict(host="127.0.0.1", port=5432, user="futures",
          password="qhyc_dev_pwd_2026", dbname="futures")


def cost(sym, mult, lots=1):
    t, f = SPEC[_PROD[sym]]
    return (2 * f + 2 * 1.0 * (t * mult)) * lots


def load_daily():
    """日线 continuous（与小时线同源同期）。缓存。"""
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


def build(bars, p, tag=""):
    out = []
    for sym, b in bars.items():
        o, h, l, c, dt = b["o"], b["h"], b["l"], b["c"], b["dt"]
        n = len(c)
        if n < p.min_bars:
            continue
        htf = _htf_direction(c, p.ema_k)
        states = np.zeros(n, dtype=int); lots = np.zeros(n, dtype=int)
        for k, d in enumerate(walk_fusion_states(o, h, l, c, htf, p)):
            if k + 2 < n:
                states[k + 2] = d["state"]; lots[k + 2] = int(d.get("lots", 1) or 1)
        cur = None
        for i in range(n):
            st = int(states[i]); lt = int(lots[i])
            if cur is None:
                if st != 0:
                    cur = dict(dir=st, first_px=float(c[i]), entry_i=i, entry_dt=dt[i],
                               lot_pxs=[float(c[i])])
            else:
                if st == cur["dir"] and lt > len(cur["lot_pxs"]):
                    for _ in range(lt - len(cur["lot_pxs"])):
                        cur["lot_pxs"].append(float(c[i]))
                elif st != cur["dir"]:
                    cur["exit_px"] = float(c[i]); cur["exit_i"] = i; cur["exit_dt"] = dt[i]
                    cur["sym"] = sym
                    out.append(cur); cur = None
                    if st != 0:
                        cur = dict(dir=st, first_px=float(c[i]), entry_i=i, entry_dt=dt[i],
                                   lot_pxs=[float(c[i])])
        if cur is not None:
            cur["exit_px"] = float(c[-1]); cur["exit_i"] = n - 1; cur["exit_dt"] = dt[-1]
            cur["sym"] = sym; cur["open_at_end"] = True
            out.append(cur)
    print(f"  [build:{tag}] 头寸 {len(out)}", flush=True)
    return out


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


def summarize(tag, r, with_gates=True):
    L = pd.DataFrame([dict(sym=x["p"]["sym"], dt=pd.Timestamp(x["p"]["entry_dt"]), Y=x["Y"],
                           gross=x["gross"], cost=x["cost"], lots=len(x["p"]["lot_pxs"]))
                      for x in r["rows"]])
    if not len(L):
        print(f"  {tag}: 无交易")
        return None
    isv = L[L.dt <= IS_SPLIT].Y.sum(); oosv = L[L.dt > IS_SPLIT].Y.sum()
    e = L[L.dt <= HALF].Y.sum(); l2 = L[L.dt > HALF].Y.sum()
    oosL = L[L.dt > IS_SPLIT]
    t5 = oosL.groupby("sym").Y.sum().nlargest(5).index.tolist() if len(oosL) else []
    no5 = oosL[~oosL.sym.isin(t5)].Y.sum() if len(oosL) else 0.0
    ratio = 100 * L.cost.sum() / max(L.gross.sum(), 1e-9)
    g1 = L[L.lots == 1]; g2 = L[L.lots == 2]
    print(f"  {tag}")
    print(f"    n={r['executed']:5d} 净{r['net']/1e4:+8.1f}万 胜{r['win']*100:5.1f}% "
          f"DD{r['mdd']/1e4:6.1f}万 MAR{r['mar']:+6.2f} 成本/毛利{ratio:5.0f}%")
    print(f"    毛利{L.gross.sum()/1e4:+8.1f}万 成本{L.cost.sum()/1e4:7.1f}万 | "
          f"单手{(g1.lots==1).sum()}笔{g1.Y.sum()/1e4:+7.1f}万(胜{(g1.Y>0).mean()*100 if len(g1) else 0:.1f}%) "
          f"加码{(g2.lots==2).sum()}笔{g2.Y.sum()/1e4:+7.1f}万(胜{(g2.Y>0).mean()*100 if len(g2) else 0:.1f}%)")
    if with_gates:
        yr = L.groupby(L.dt.dt.year).Y.sum()
        print(f"    IS{isv/1e4:+7.1f}万 OOS{oosv/1e4:+7.1f}万 {'✅同号' if np.sign(isv)==np.sign(oosv) else '❌翻号'}"
              f" | 半样本 前{e/1e4:+6.1f}/后{l2/1e4:+6.1f} | 去TOP5后OOS{no5/1e4:+7.1f}万")
        print("    逐年 " + " ".join(f"{k}:{v/1e4:+.0f}" for k, v in yr.items()))
    return dict(tag=tag, net=r["net"], n=r["executed"], isv=isv, oosv=oosv,
                same=int(np.sign(isv) == np.sign(oosv)), oos_no5=no5, ratio=ratio)


def main():
    p0 = FusionBacktestParams()
    print("=" * 92)
    print("STEP 1  基线对照（小时线 ema_k=140）")
    print("=" * 92)
    hb = os.path.join(OUT, "hourly_bars.pkl")
    if os.path.exists(hb):
        with open(hb, "rb") as f:
            hbars = pickle.load(f)
        rbh = replay(build(hbars, p0, "hourly"))
        H = summarize("小时线 V3.4（基线）", rbh)
    else:
        H = None
        print("  [跳过] 无 hourly_bars.pkl 缓存")

    print("\n" + "=" * 92)
    print("STEP 2  日线口径（官方映射：方向层 EMA20 / 入场 EMA10；门槛不变）")
    print("=" * 92)
    dbars = load_daily()
    print(f"[data] 日线 {len(dbars)} 品种")
    pd_ = FusionBacktestParams()
    pd_.ema_k = 20          # 方向层：日线 EMA20 ≈ 小时 EMA140（官方口径）
    pd_.ma_n = 10           # 入场层：日线 EMA10 ≈ 小时 EMA20（官方口径）
    pd_.min_bars = 60       # 日线预热（对应小时 160 根 ≈ 26 日，取 60 保证 ATR/EMA 收敛）
    pd_.cooldown_bars = 1   # 小时 3 根 ≈ 日线 1 根
    pd_.W = 20              # 斐波窗口：小时 60 根 ≈ 日线 20 根（按 ~3 日/根 等比）
    print(f"[params] ema_k={pd_.ema_k} ma_n={pd_.ma_n} W={pd_.W} cooldown={pd_.cooldown_bars} "
          f"min_bars={pd_.min_bars} | 不变: adx_min={pd_.adx_min} sl_atr={pd_.sl_atr} "
          f"trail_atr={pd_.trail_atr} be_r={pd_.be_r} fib_tol={pd_.fib_tol_atr} add_thr={pd_.add_thr_atr}")
    posd = build(dbars, pd_, "daily_offical")
    rd = replay(posd)
    D = summarize("日线（官方映射 ema20/ma10/W20）", rd)

    print("\n" + "=" * 92)
    print("STEP 3  对照组：参数直接照搬（ema_k=140/ma_n=20/W=60 不缩放）——排除口径争议")
    print("=" * 92)
    px = FusionBacktestParams()
    px.min_bars = 60
    px.cooldown_bars = 1
    posx = build(dbars, px, "daily_raw140")
    rx = replay(posx)
    X = summarize("日线（不缩放 ema140/ma20/W60）", rx)

    print("\n" + "=" * 92)
    print("STEP 4  判决")
    print("=" * 92)
    print(f"  {'配置':<34}{'成交':>7}{'净利(万)':>11}{'成本/毛利':>10}{'IS(万)':>10}{'OOS(万)':>10}{'同号':>7}")
    for res in (H, D, X):
        if res:
            print(f"  {res['tag'][:32]:<34}{res['n']:>7}{res['net']/1e4:>11.1f}"
                  f"{res['ratio']:>9.0f}%{res['isv']/1e4:>10.1f}{res['oosv']/1e4:>10.1f}"
                  f"{'  ✅' if res['same'] else '  ❌':>7}")


if __name__ == "__main__":
    main()
