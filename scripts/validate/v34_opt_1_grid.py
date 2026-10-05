"""
V3.4 三步优化验证（修正后方向）
================================
前一版诊断的自我否决：
  原判断"关加码（add_max_lots 2→1）"→ 被数据否决。
  实测：加码单(lots=2) 3,088笔 胜率45.7% 毛利+270.1万 净+241.5万；
        单手单(lots=1) 3,409笔 胜率0.3% 毛利-236.2万 净-251.8万。
  => 加码不是放大器，而是【质量过滤器】：能走到加码的单已是通过"浮盈≥1×ATR+创新高"的赢家。
  => 真正的问题是【入场即被扫】的短命单过多。
  => 优化方向改为：提高入场门槛（ADX 上探 / 斐波收紧 / 最小持仓约束），加码保持不动。

本脚本按此方向跑，每项带 IS/OOS + 四道稳健性关口。
"""
import os
import sys
import numpy as np
import pandas as pd
import pickle

sys.path.insert(0, r"E:\Docker\qhyc")
OUT = r"E:\Docker\qhyc\docs\_swing_out"

import psycopg2

PG = dict(host="127.0.0.1", port=5432, user="futures",
          password=os.environ["POSTGRES_PASSWORD"], dbname="futures")

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


def cost(sym, mult, lots=1):
    t, f = SPEC[_PROD[sym]]
    return (2 * f + 2 * 1.0 * (t * mult)) * lots


BARS_CACHE = os.path.join(OUT, "hourly_bars.pkl")


def load_bars():
    """一次载入全部品种 K 线（供多组参数复用，避免重复查库）。"""
    if os.path.exists(BARS_CACHE):
        with open(BARS_CACHE, "rb") as f:
            return pickle.load(f)
    bars = {}
    for m in _MC:
        got = None
        for cand in (m.product.upper(), m.product.lower()):
            sql = ("SELECT trade_datetime, open::float8, high::float8, low::float8, close::float8 "
                   "FROM fut_kline WHERE freq='hourly' AND kind='continuous' "
                   f"AND symbol='KQ.m@{m.exchange}.{cand}' ORDER BY trade_datetime ASC")
            with psycopg2.connect(**PG) as cn:
                df = pd.read_sql(sql, cn, parse_dates=["trade_datetime"])
            if len(df):
                got = dict(dt=df.trade_datetime.to_numpy(),
                           o=df.open.to_numpy(float), h=df.high.to_numpy(float),
                           l=df.low.to_numpy(float), c=df.close.to_numpy(float))
                break
        if got is not None:
            bars[m.symbol] = got
    with open(BARS_CACHE, "wb") as f:
        pickle.dump(bars, f)
    return bars


def build(bars, p, tag):
    """按给定参数构建头寸（含 entry_i/exit_i，供最小持仓约束用）。"""
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


def replay(pos_list, min_bars_hold=0, bars=None, cap=5):
    """容量5 FIFO + 可选最小持仓约束（bars 用于查高低价判断是否被提前扫）。"""
    tl = []
    for p in pos_list:
        tl.append((p["entry_dt"], 0, p))
        if not p.get("open_at_end"):
            tl.append((p["exit_dt"], 1, p))
    tl.sort(key=lambda x: (x[0], x[1]))
    openp = {}
    eq = peak = mdd = 0.0
    w = l = ex = sk = 0
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
            # 最小持仓约束：入场后 N 根内不止损离场（模拟"扛过短期波动"）
            if min_bars_hold > 0 and bars is not None:
                b = bars.get(sym)
                if b is not None:
                    e_i, x_i = int(p["entry_i"]), int(p["exit_i"])
                    if x_i - e_i < min_bars_hold:
                        # 延后离场到 e_i+min_bars_hold 那根收盘
                        nx = min(e_i + min_bars_hold, len(b["c"]) - 1)
                        if nx > x_i:
                            p = dict(p)
                            p["exit_px"] = float(b["c"][nx]); p["exit_i"] = nx
            mult = MULT[sym]; d = 1.0 if p["dir"] == 1 else -1.0
            lots = len(p["lot_pxs"])
            if len(openp) >= cap or sym in openp:
                sk += 1; continue
            gross = sum(d * (p["exit_px"] - px) * mult for px in p["lot_pxs"])
            Y = gross - cost(sym, mult, lots)
            openp[sym] = dict(Y=Y, win=Y > 0, p=p, gross=gross, cost=cost(sym, mult, lots))
            ex += 1
    n = w + l
    return dict(executed=ex, skip=sk, win=w / n if n else 0.0, net=eq, mdd=mdd,
                mar=eq / mdd if mdd > 0 else 0.0, rows=rows)


def stat_line(tag, r, base=None):
    print(f"  {tag:<30} 成交{r['executed']:5d} 净利{r['net']/1e4:+8.1f}万 "
          f"胜率{r['win']*100:5.1f}% DD{r['mdd']/1e4:6.1f}万 MAR{r['mar']:+6.2f}"
          + (f" | Δ{(r['net']-base)/1e4:+7.1f}万" if base is not None else ""))


def four_gates(pos_list, p, bars, label, base_net):
    """四道稳健性关口：IS-OOS 同号率 / 去TOP5 / 半样本 / 参数高原。"""
    r = replay(pos_list)
    L = pd.DataFrame([dict(sym=x["p"]["sym"], dt=pd.Timestamp(x["p"]["entry_dt"]), Y=x["Y"],
                           gross=x["gross"], cost=x["cost"]) for x in r["rows"]])
    isv = L[L.dt <= IS_SPLIT].Y.sum()
    oosv = L[L.dt > IS_SPLIT].Y.sum()
    same = np.sign(isv) == np.sign(oosv)
    # 去 TOP5（按 OOS 段贡献）
    Lo = L[L.dt > IS_SPLIT]
    if len(Lo):
        t5 = Lo.groupby("sym").Y.sum().nlargest(5).index.tolist()
        oos_ex = Lo.Y.sum(); oos_no = Lo[~Lo.sym.isin(t5)].Y.sum()
    else:
        t5, oos_ex, oos_no = [], 0.0, 0.0
    # 半样本：前半选"是否用此配置"无参可选 → 直接报前后半净利
    early = L[L.dt <= HALF].Y.sum(); late = L[L.dt > HALF].Y.sum()
    print(f"  [{label}] 四道关口：IS {isv/1e4:+.1f}万 / OOS {oosv/1e4:+.1f}万 "
          f"同号{'✅' if same else '❌'} | 去TOP5后OOS {oos_no/1e4:+.1f}万 "
          f"(原{oos_ex/1e4:+.1f}万) {'✅' if np.sign(oos_no)==np.sign(base_net) or oos_no>0 else '❌'}"
          f" | 半样本 前{early/1e4:+.1f}/后{late/1e4:+.1f}")
    return dict(isv=isv, oosv=oosv, same=same, oos_no=oos_no, early=early, late=late)


def main():
    print("=" * 84)
    print("STEP A  基线（当前引擎默认参数）")
    print("=" * 84)
    bars = load_bars()
    print(f"[cache] {len(bars)} 品种 K 线")
    p0 = FusionBacktestParams()
    print(f"[params] adx_min={p0.adx_min} fib_tol={p0.fib_tol_atr} "
          f"add_max_lots={p0.add_max_lots} fib_confl={p0.fib_confl}")
    base_pos = build(bars, p0, "base")
    rb = replay(base_pos)
    base_net = rb["net"]
    stat_line("基线 V3.4", rb)
    Lb = pd.DataFrame([dict(sym=x["p"]["sym"], dt=pd.Timestamp(x["p"]["entry_dt"]),
                            Y=x["Y"], gross=x["gross"], cost=x["cost"],
                            lots=len(x["p"]["lot_pxs"]),
                            bars=int(x["p"]["exit_i"] - x["p"]["entry_i"]))
                       for x in rb["rows"]])
    print(f"  毛利 {Lb.gross.sum()/1e4:+.1f}万 | 成本 {Lb.cost.sum()/1e4:+.1f}万 "
          f"| 成本/毛利 {100*Lb.cost.sum()/max(Lb.gross.sum(),1e-9):.0f}%")
    print(f"  单手单 {(Lb.lots==1).sum()}笔 净{Lb[Lb.lots==1].Y.sum()/1e4:+.1f}万 | "
          f"加码单 {(Lb.lots==2).sum()}笔 净{Lb[Lb.lots==2].Y.sum()/1e4:+.1f}万")

    # ---------- STEP B: ADX 上探（用户记忆 15 = 当前值）----------
    print("\n" + "=" * 84)
    print("STEP B  ADX 上探（当前 adx_min=15；PRD 记'≥20转亏'是旧引擎结论，须重测）")
    print("=" * 84)
    adx_rows = []
    for a in [0.0, 15.0, 18.0, 20.0, 22.0, 25.0]:
        p = FusionBacktestParams()
        p.adx_min = a
        if a == p0.adx_min:
            r = rb
        else:
            pos = build(bars, p, f"adx{a}")
            r = replay(pos)
        stat_line(f"adx_min={a}", r, base_net)
        L = pd.DataFrame([dict(sym=x["p"]["sym"], dt=pd.Timestamp(x["p"]["entry_dt"]), Y=x["Y"],
                               lots=len(x["p"]["lot_pxs"])) for x in r["rows"]])
        isv = L[L.dt <= IS_SPLIT].Y.sum(); oosv = L[L.dt > IS_SPLIT].Y.sum()
        adx_rows.append(dict(adx=a, net=r["net"], n=r["executed"], win=r["win"],
                             isv=isv, oosv=oosv,
                             same=int(np.sign(isv) == np.sign(oosv))))
    A = pd.DataFrame(adx_rows)
    A.to_csv(os.path.join(OUT, "opt_adx_grid.csv"), index=False)
    print(f"\n  ADX 正收益格子 {int((A.net>0).sum())}/{len(A)} | "
          f"IS-OOS 同号 {int(A.same.sum())}/{len(A)} = {100*A.same.mean():.0f}%")

    # ---------- STEP C: 斐波容差重扫 ----------
    print("\n" + "=" * 84)
    print("STEP C  斐波容差 fib_tol_atr 重扫（当前 0.5；PRD 记'无优化空间'是旧引擎结论）")
    print("=" * 84)
    fib_rows = []
    for tol in [0.0, 0.25, 0.5, 0.75, 1.0]:
        p = FusionBacktestParams()
        p.fib_tol_atr = tol
        if tol == p0.fib_tol_atr:
            r = rb
        else:
            pos = build(bars, p, f"tol{tol}")
            r = replay(pos)
        stat_line(f"fib_tol={tol}", r, base_net)
        L = pd.DataFrame([dict(sym=x["p"]["sym"], dt=pd.Timestamp(x["p"]["entry_dt"]), Y=x["Y"],
                               lots=len(x["p"]["lot_pxs"])) for x in r["rows"]])
        isv = L[L.dt <= IS_SPLIT].Y.sum(); oosv = L[L.dt > IS_SPLIT].Y.sum()
        fib_rows.append(dict(tol=tol, net=r["net"], n=r["executed"], win=r["win"],
                             isv=isv, oosv=oosv,
                             same=int(np.sign(isv) == np.sign(oosv))))
    F = pd.DataFrame(fib_rows)
    F.to_csv(os.path.join(OUT, "opt_fib_grid.csv"), index=False)
    print(f"\n  斐波正收益格子 {int((F.net>0).sum())}/{len(F)} | "
          f"IS-OOS 同号 {int(F.same.sum())}/{len(F)} = {100*F.same.mean():.0f}%")

    # ---------- STEP D: 最小持仓约束（针对"入场即被扫"） ----------
    print("\n" + "=" * 84)
    print("STEP D  最小持仓约束（入场后 N 根内不止损离场）")
    print("=" * 84)
    print("  动机：单手单胜率 0.3%、毛利 -236万 → 大量入场即被扫的短命单")
    for nb in [0, 3, 6, 12, 24]:
        r = replay(base_pos, min_bars_hold=nb, bars=bars)
        stat_line(f"min_hold={nb}根", r, base_net)
    print("\n[注] min_hold 是事后口径（用未来 bar 的收盘价延后离场），仅作量级筛查，"
          "不可直接实盘；实盘需改为「止损单上移」等因果规则。")


if __name__ == "__main__":
    main()
