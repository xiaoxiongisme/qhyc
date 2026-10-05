"""
集中度专项：因果版本验证（剔除事后口径）
==========================================
【本轮最重要发现】亏损有清晰的时间结构：
  加码单按持仓时长：6-24h 胜率11.2% 合计-37.6万 | 3-7天 胜率57.2% 合计+150.3万
                  | 7-14天 胜率79.8% +105.9万 | >14天 胜率94.5% +25.6万
  单手单任何时长都亏（胜率0.0-2.6%，笔均毛利稳定-650~-700元，与时长无关）
  => 机制：V3.4 信号在短周期内不兑现，只有穿越约3天的趋势才有效；
           单手单是"入场即注定亏损"，不是"持仓太久才亏"。

【危险】按"最终持仓时长"筛选（砍 span<72h）得 +243.7万、成本毛利比7%、IS/OOS同号
  —— 但那是【事后口径】：入场时不知道会持有多久。这是未来函数，必须作废。

本脚本验证【因果版本】：
  C1 信号确认：入场后 N 根内浮盈未达 X×ATR 则离场（可用"入场后第 N 根收盘价"判定，实时可算）
  C2 加码确认：只统计"能触发加码"的信号 —— 但加码是入场后才发生的，也属事后。
     ⇒ C2 不可用为实盘规则，只能用作"入场端过滤是否有可能"的探针。
  C3 真正因果的替代：把"快速止损"改成"分批止损/时间止损"（入场时即可挂单）
  C4 ADX 与持仓时长的交互：是否强趋势入场才走长周期（若成立，可用入场时 ADX 过滤）

所有规则必须满足：入场时可判定 + 不使用未来 bar。
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
from app.strategies.fusion_signal import walk_fusion_states, adx14, atr14

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


def cost(sym, mult, lots=1):
    t, f = SPEC[_PROD[sym]]
    return (2 * f + 2 * 1.0 * (t * mult)) * lots


def load_bars():
    p = os.path.join(OUT, "hourly_bars.pkl")
    if os.path.exists(p):
        with open(p, "rb") as f:
            return pickle.load(f)
    bars = {}
    for m in _MC:
        for cand in (m.product.upper(), m.product.lower()):
            sql = ("SELECT trade_datetime, open::float8, high::float8, low::float8, close::float8 "
                   "FROM fut_kline WHERE freq='hourly' AND kind='continuous' "
                   f"AND symbol='KQ.m@{m.exchange}.{cand}' ORDER BY trade_datetime ASC")
            with psycopg2.connect(**PGX) as cn:
                df = pd.read_sql(sql, cn, parse_dates=["trade_datetime"])
            if len(df):
                bars[m.symbol] = dict(dt=df.trade_datetime.to_numpy(),
                                       o=df.open.to_numpy(float), h=df.high.to_numpy(float),
                                       l=df.low.to_numpy(float), c=df.close.to_numpy(float))
                break
    with open(p, "wb") as f:
        pickle.dump(bars, f)
    return bars


PGX = dict(host="127.0.0.1", port=5432, user="futures",
           password=os.environ["POSTGRES_PASSWORD"], dbname="futures")


def build(bars, p, tag=""):
    out = []
    for sym, b in bars.items():
        o, h, l, c, dt = b["o"], b["h"], b["l"], b["c"], b["dt"]
        n = len(c)
        if n < p.min_bars:
            continue
        htf = _htf_direction(c, p.ema_k)
        atr = atr14(h, l, c, p.atr_n)
        adx = adx14(h, l, c, int(getattr(p, "adx_n", 14)))
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
                               e_atr=float(atr[i]) if np.isfinite(atr[i]) else np.nan,
                               adx_at_entry=float(adx[i]) if np.isfinite(adx[i]) else np.nan,
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
                                   e_atr=float(atr[i]) if np.isfinite(atr[i]) else np.nan,
                                   adx_at_entry=float(adx[i]) if np.isfinite(adx[i]) else np.nan,
                                   lot_pxs=[float(c[i])])
        if cur is not None:
            cur["exit_px"] = float(c[-1]); cur["exit_i"] = n - 1; cur["exit_dt"] = dt[-1]
            cur["sym"] = sym; cur["open_at_end"] = True
            out.append(cur)
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


def summarize(tag, r, base=None, L=None):
    if L is None:
        L = pd.DataFrame([dict(sym=x["p"]["sym"], dt=pd.Timestamp(x["p"]["entry_dt"]), Y=x["Y"],
                               gross=x["gross"], cost=x["cost"], lots=len(x["p"]["lot_pxs"]))
                          for x in r["rows"]])
    isv = L[L.dt <= IS_SPLIT].Y.sum(); oosv = L[L.dt > IS_SPLIT].Y.sum()
    oosL = L[L.dt > IS_SPLIT]
    t5 = oosL.groupby("sym").Y.sum().nlargest(5).index.tolist() if len(oosL) else []
    no5 = oosL[~oosL.sym.isin(t5)].Y.sum() if len(oosL) else 0.0
    ratio = 100 * L.cost.sum() / max(L.gross.sum(), 1e-9)
    print(f"  {tag:<26} n={r['executed']:5d} 净{r['net']/1e4:+8.1f}万 胜{r['win']*100:5.1f}% "
          f"DD{r['mdd']/1e4:6.1f}万 MAR{r['mar']:+6.2f} 成本/毛利{ratio:5.0f}% | "
          f"IS{isv/1e4:+7.1f} OOS{oosv/1e4:+7.1f} {'✅' if np.sign(isv)==np.sign(oosv) else '❌'} "
          f"| 去TOP5 OOS{no5/1e4:+7.1f}万")
    return dict(tag=tag, net=r["net"], n=r["executed"], isv=isv, oosv=oosv,
                same=int(np.sign(isv) == np.sign(oosv)), oos_no5=no5, ratio=ratio)


def main():
    p0 = FusionBacktestParams()
    bars = load_bars()
    print(f"[data] {len(bars)} 品种 | params adx_min={p0.adx_min} fib_tol={p0.fib_tol_atr} "
          f"add_thr={p0.add_thr_atr} add_max_lots={p0.add_max_lots}")
    pos = build(bars, p0)
    rb = replay(pos)
    print("\n" + "=" * 96)
    print("STEP 1  基线 + 事后口径对照（作废，仅证明时间结构存在）")
    print("=" * 96)
    B = summarize("基线 V3.4", rb)
    T = pd.DataFrame([dict(sym=x["p"]["sym"], dt=pd.Timestamp(x["p"]["entry_dt"]), Y=x["Y"],
                           gross=x["gross"], cost=x["cost"], lots=len(x["p"]["lot_pxs"]),
                           e_i=int(x["p"]["entry_i"]), x_i=int(x["p"]["exit_i"]))
                      for x in rb["rows"]])
    span_h = []
    for x in rb["rows"]:
        span_h.append((pd.Timestamp(x["p"]["exit_dt"]) - pd.Timestamp(x["p"]["entry_dt"])).total_seconds() / 3600)
    T["span_h"] = span_h
    for th in [24, 48, 72]:
        k = T[T.span_h >= th]
        r = dict(executed=len(k), net=k.Y.sum(), win=(k.Y > 0).mean(), mdd=np.nan, mar=0)
        summarize(f"[事后·作废] span>={th}h", r, L=k[["sym", "dt", "Y", "gross", "cost", "lots"]])

    print("\n" + "=" * 96)
    print("STEP 2  C4 因果探针：入场时 ADX 能否预测「会走长周期」")
    print("=" * 96)
    adx_at = []
    for x in rb["rows"]:
        adx_at.append(x["p"].get("adx_at_entry", np.nan))
    T["adx_e"] = adx_at
    Tl = T.dropna(subset=["adx_e"])
    if len(Tl):
        Tl = Tl.copy()
        Tl["long"] = (Tl.span_h >= 72).astype(int)
        print(f"  样本 n={len(Tl)} | 长周期(>=72h)占比 {Tl.long.mean():.1%}")
        for lo, hi in [(0, 15), (15, 20), (20, 25), (25, 30), (30, 100)]:
            g = Tl[(Tl.adx_e >= lo) & (Tl.adx_e < hi)]
            if len(g) < 30:
                continue
            print(f"    ADX入场 ∈[{lo:2d},{hi:2d}): n={len(g):4d} 长周期占比 {g.long.mean():5.1%} "
                  f"净利{g.Y.sum()/1e4:+7.1f}万 笔均毛利{g.gross.mean():+6.0f}元")
        from scipy.stats import spearmanr
        rho, pv = spearmanr(Tl.adx_e, Tl.span_h)
        print(f"\n  spearman(入场ADX, 持仓小时数) = {rho:+.3f} (p={pv:.3f}, n={len(Tl)})")
        # 长短分组的净利对照
        Tl["grp"] = np.where(Tl.span_h >= 72, "长持仓", "短持仓")
        for g, gg in Tl.groupby("grp"):
            print(f"    {g}: n={len(gg):4d} 净{gg.Y.sum()/1e4:+7.1f}万 "
                  f"成本/毛利 {100*gg.cost.sum()/max(gg.gross.sum(),1e-9):.0f}%")

    print("\n" + "=" * 96)
    print("STEP 3  C1 因果规则：入场后 N 根收盘仍未达 X×ATR 则离场（实时可算）")
    print("=" * 96)
    print("  说明：这是【时间止损】。入场时挂单即可执行，不使用未来 bar。")
    for n_bars, x_atr in [(6, 0.0), (6, 0.5), (12, 0.0), (12, 0.5), (24, 0.0), (24, 0.5)]:
        mod = []
        for p_ in pos:
            q = dict(p_)
            e_i, x_i = int(p_["entry_i"]), int(p_["exit_i"])
            b = bars.get(q["sym"])
            if b is None or np.isnan(q.get("e_atr", np.nan)) or q["e_atr"] <= 0:
                mod.append(q); continue
            ck = min(e_i + n_bars, len(b["c"]) - 1)
            if ck > x_i:                       # 原持仓已超过检查点，不触发
                mod.append(q); continue
            d = 1.0 if q["dir"] == 1 else -1.0
            prog = d * (b["c"][ck] - q["first_px"])          # 到检查点的浮盈（点数）
            if prog < x_atr * q["e_atr"]:
                q["exit_px"] = float(b["c"][ck]); q["exit_i"] = ck   # 提前离场
            mod.append(q)
        r = replay(mod)
        summarize(f"N={n_bars}根 且浮盈<{x_atr}×ATR", r, B["net"])

    print("\n" + "=" * 96)
    print("STEP 4  集中度：能否用分散化解决？（回答用户第 3 项）")
    print("=" * 96)
    print("  STEP1 已否决品种筛选（IS盈利名单在OOS只+4.7万 < 基准+10.5万）")
    print("  本步检验：单纯『多品种等权 vs 按信号数加权』能否降低集中度")
    for cap in [3, 5, 8, 12, 20]:
        r = replay(pos, cap=cap)
        L = pd.DataFrame([dict(sym=x["p"]["sym"], dt=pd.Timestamp(x["p"]["entry_dt"]), Y=x["Y"])
                          for x in r["rows"]])
        if not len(L):
            continue
        bys = L.groupby("sym").Y.sum()
        n_sym = (bys > 0).sum()
        hhi = float((bys / max(bys.abs().sum(), 1e-9)).pow(2).sum())
        print(f"    容量cap={cap:2d}: n={r['executed']:5d} 净{r['net']/1e4:+8.1f}万 "
              f"参与品种{n_sym:2d} HHI={hhi:.3f} 剔除TOP5后{L[~L.sym.isin(bys.nlargest(5).index)].Y.sum()/1e4:+7.1f}万")


if __name__ == "__main__":
    main()
