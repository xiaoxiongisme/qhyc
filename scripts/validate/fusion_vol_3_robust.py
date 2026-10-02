"""
Q1 稳健性终检 + Q3 逐 bar 反手模拟
====================================
Q1a 首扫结果（波动<=pX 才开仓）：
  p50 +36.5万 / p60 -6.6万 / p70 +7.2万 / p80 +22.2万 / p90 -6.5万  ← 非单调，无高原
  => 必须查：IS/OOS 是否同号 / 半样本是否翻负 / 是不是单点尖峰
Q3 亏损反手 2:1：首版用"假定都打到目标"的乐观口径（作废），
   本版改为逐 bar 真实模拟：反手单有自己的止损与 2:1 止盈。
"""
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, r"E:\Docker\qhyc")
OUT = r"E:\Docker\qhyc\docs\_swing_out"

import psycopg2
import pickle

PG = dict(host="127.0.0.1", port=5432, user="futures",
          password="qhyc_dev_pwd_2026", dbname="futures")

from app.core.config import get_settings
from app.backtest.fusion_backtest import FusionBacktestParams
from app.strategies.fusion_signal import atr14

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


def load_hourly(prod, exch):
    for cand in (prod.upper(), prod.lower()):
        sql = ("SELECT trade_datetime, open::float8, high::float8, low::float8, close::float8 "
               "FROM fut_kline WHERE freq='hourly' AND kind='continuous' "
               f"AND symbol='KQ.m@{exch}.{cand}' ORDER BY trade_datetime ASC")
        with psycopg2.connect(**PG) as cn:
            df = pd.read_sql(sql, cn, parse_dates=["trade_datetime"])
        if len(df):
            return df
    return None


# ------------------------------------------------------------ Q1 稳健性
def replay(pos_list, filt=None, cap=5):
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
            if filt is not None and not filt(p):
                sk += 1; continue
            if len(openp) >= cap or sym in openp:
                sk += 1; continue
            mult = MULT[sym]; d = 1.0 if p["dir"] == 1 else -1.0
            lots = len(p["lot_pxs"])
            gross = sum(d * (p["exit_px"] - px) * mult for px in p["lot_pxs"])
            Y = gross - cost(sym, mult, lots)
            openp[sym] = dict(Y=Y, win=Y > 0, p=p)
            ex += 1
    n = w + l
    return dict(executed=ex, skip=sk, win=w / n if n else 0.0, net=eq, mdd=mdd,
                mar=eq / mdd if mdd > 0 else 0.0, rows=rows)


def q1_robust():
    print("=" * 80)
    print("Q1  波动过滤器 —— 稳健性终检（首扫非单调，必须查尖峰）")
    print("=" * 80)
    with open(os.path.join(OUT, "v34_positions.pkl"), "rb") as f:
        pos = pickle.load(f)
    base = replay(pos)
    print(f"[基线] 成交{base['executed']} 净利{base['net']/1e4:+.1f}万 "
          f"胜率{base['win']*100:.1f}% DD{base['mdd']/1e4:.1f}万 MAR{base['mar']:.2f}")

    Vr = pd.Series([p["vol_rel"] for p in pos if p.get("vol_rel") is not np.nan
                    and np.isfinite(p.get("vol_rel", np.nan))])
    print(f"[波动参照] n={len(Vr)} 中位={Vr.median():.2f}")

    print("\n--- F1 细网格扫参（q 从 0.30 到 0.95，看是否有高原）---")
    print(f"{'q':>6}{'成交':>7}{'净利(万)':>11}{'DD(万)':>9}{'MAR':>8}{'IS(万)':>10}{'OOS(万)':>10}{'同号':>6}")
    grid = []
    for q in [0.30, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]:
        thr = float(Vr.quantile(q))
        r = replay(pos, filt=lambda pp, t=thr: (pp.get("vol_rel") is None
                                                or not np.isfinite(pp["vol_rel"])
                                                or pp["vol_rel"] <= t))
        L = pd.DataFrame([dict(dt=x["p"]["entry_dt"], Y=x["Y"]) for x in r["rows"]])
        if len(L) == 0:
            continue
        isv = L[L.dt <= IS_SPLIT].Y.sum()
        oosv = L[L.dt > IS_SPLIT].Y.sum()
        same = "Y" if np.sign(isv) == np.sign(oosv) else "N"
        grid.append(dict(q=q, thr=thr, n=r["executed"], net=r["net"],
                         mdd=r["mdd"], mar=r["mar"], isv=isv, oosv=oosv))
        print(f"{q:>6.2f}{r['executed']:>7}{r['net']/1e4:>11.1f}{r['mdd']/1e4:>9.1f}"
              f"{r['mar']:>8.2f}{isv/1e4:>10.1f}{oosv/1e4:>10.1f}{same:>6}")
    G = pd.DataFrame(grid)
    G.to_csv(os.path.join(OUT, "q1_vol_filter_grid.csv"), index=False)

    pos_g = (G.net > 0).sum()
    same_g = (np.sign(G.isv) == np.sign(G.oosv)).sum()
    print(f"\n  全样本正收益格子: {pos_g}/{len(G)} = {100*pos_g/len(G):.0f}%")
    print(f"  IS-OOS 同号:      {same_g}/{len(G)} = {100*same_g/len(G):.0f}%")
    print(f"  净利范围 {G.net.min()/1e4:+.1f} ~ {G.net.max()/1e4:+.1f} 万 "
          f"→ 尖峰比 {(G.net.max()-G.net.min())/max(abs(np.median(G.net)),1e-9):.1f}x")

    # F2 半样本：前半段选 q，后半段验
    print("\n--- F2 半样本（2015-2019 选参 → 2020-2025 验）---")
    early = [p for p in pos if pd.Timestamp(p["entry_dt"]) <= HALF]
    late = [p for p in pos if pd.Timestamp(p["entry_dt"]) > HALF]
    b_e = replay(early); b_l = replay(late)
    print(f"  基线 前半{ b_e['net']/1e4:+.1f}万({b_e['executed']}笔) | "
          f"后半{b_l['net']/1e4:+.1f}万({b_l['executed']}笔)")
    Vre = pd.Series([p["vol_rel"] for p in early if p.get("vol_rel") is not None
                     and np.isfinite(p.get("vol_rel", np.nan))])
    Vrl = pd.Series([p["vol_rel"] for p in late if p.get("vol_rel") is not None
                     and np.isfinite(p.get("vol_rel", np.nan))])
    best_q, best_v = None, -1e18
    for q in [0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90]:
        thr = float(Vre.quantile(q))
        r = replay(early, filt=lambda pp, t=thr: (pp.get("vol_rel") is None
                                                  or not np.isfinite(pp["vol_rel"])
                                                  or pp["vol_rel"] <= t))
        if r["net"] > best_v:
            best_v, best_q = r["net"], q
    thr_l = float(Vrl.quantile(best_q))
    r_l = replay(late, filt=lambda pp, t=thr_l: (pp.get("vol_rel") is None
                                                 or not np.isfinite(pp["vol_rel"])
                                                 or pp["vol_rel"] <= t))
    print(f"  前半最优 q={best_q}（{best_v/1e4:+.1f}万）→ 后半同参 {r_l['net']/1e4:+.1f}万 "
          f"({'同号' if np.sign(best_v)==np.sign(r_l['net']) else '✗ 翻负'})")

    # F3 去 TOP5 集中度
    print("\n--- F3 剔除 TOP5 贡献品种（OOS 段）---")
    Lo = pd.DataFrame([dict(sym=x["p"]["sym"], dt=x["p"]["entry_dt"], Y=x["Y"])
                       for x in replay([p for p in pos if pd.Timestamp(p["entry_dt"]) > IS_SPLIT],
                                       filt=lambda pp: (pp.get("vol_rel") is None
                                                        or not np.isfinite(pp["vol_rel"])
                                                        or pp["vol_rel"]
                                                        <= float(Vr.quantile(0.50))))["rows"]])
    if len(Lo):
        top5 = Lo.groupby("sym").Y.sum().nlargest(5).index.tolist()
        keep = Lo[~Lo.sym.isin(top5)]
        print(f"  OOS 全品种 {Lo.Y.sum()/1e4:+.1f}万 → 去TOP5 {keep.Y.sum()/1e4:+.1f}万 "
              f"（TOP5={top5}）")


# ------------------------------------------------------------ Q3 逐 bar
def q3_flip():
    print("\n" + "=" * 80)
    print("Q3  亏损 X·ATR 后反手，止盈 2X —— 逐 bar 真实模拟")
    print("=" * 80)
    with open(os.path.join(OUT, "v34_positions.pkl"), "rb") as f:
        pos = pickle.load(f)
    p = FusionBacktestParams()
    # 预载每品种 bar（逐 bar 模拟需要 high/low）
    bars = {}
    for m in _MC:
        df = load_hourly(m.product, m.exchange)
        if df is None or len(df) < p.min_bars:
            continue
        bars[m.symbol] = dict(
            dt=df.trade_datetime.to_numpy(),
            h=df.high.to_numpy(float), l=df.low.to_numpy(float),
            c=df.close.to_numpy(float))
    print(f"[data] 载入 {len(bars)} 品种 bar 数据")

    base = replay(pos)
    print(f"[基线] 净利{base['net']/1e4:+.1f}万 成交{base['executed']} "
          f"DD{base['mdd']/1e4:.1f}万")

    rows = []
    for kx in [0.5, 1.0, 1.5, 2.0]:
        rr = 2.0
        add_y = 0.0
        add_n = 0
        add_win = 0
        for pp in pos:
            if pp.get("open_at_end"):
                continue
            sym = pp["sym"]
            bd = bars.get(sym)
            if bd is None:
                continue
            # 原单亏损幅度（点数）
            d0 = 1.0 if pp["dir"] == 1 else -1.0
            loss_pts = d0 * (pp["exit_px"] - pp["first_px"])
            # 用入场时 ATR 归一
            e_i = int(pp["entry_i"])
            w = bd["h"][max(0, e_i - 14):e_i] - bd["l"][max(0, e_i - 14):e_i]
            atr = float(pd.Series(w).mean()) if len(w) else 0.0
            if atr <= 0 or loss_pts <= kx * atr:
                continue
            # 反手：从原单离场点开反向仓，止盈 2*kx*ATR，止损 1*kx*ATR
            px0 = pp["exit_px"]
            thr_pts = kx * atr
            newdir = -d0
            tgt = px0 + newdir * rr * thr_pts
            stp = px0 - newdir * thr_pts
            # 在原单离场后的 bar 里找触发（最多 30 根）
            x_i = int(pp["exit_i"])
            seg = slice(x_i + 1, min(x_i + 31, len(bd["c"])))
            exit_px, hit = None, None
            for j in range(seg.start, seg.stop):
                hi, lo = bd["h"][j], bd["l"][j]
                if newdir > 0:
                    if lo <= stp:
                        exit_px, hit = stp, "stop"; break
                    if hi >= tgt:
                        exit_px, hit = tgt, "target"; break
                else:
                    if hi >= stp:
                        exit_px, hit = stp, "stop"; break
                    if lo <= tgt:
                        exit_px, hit = tgt, "target"; break
            if exit_px is None:
                continue      # 30 根内没触发 = 不做这单
            mult = MULT[sym]
            gross = newdir * (exit_px - px0) * mult
            Y = gross - cost(sym, mult, 1)
            add_y += Y; add_n += 1; add_win += int(Y > 0)
            rows.append(dict(kx=kx, sym=sym, Y=Y, hit=hit, thr_pts=thr_pts))
        if add_n == 0:
            print(f"  反手线 {kx}×ATR: 无触发")
            continue
        wr = add_win / add_n
        # 与基线合并（近似：反手单不占容量，串行叠加）
        comb = base["net"] + add_y
        print(f"  反手线={kx}×ATR 2:1 | 反手单{add_n:5d}笔 胜率{wr*100:5.1f}% "
              f"合计{add_y/1e4:+8.1f}万 | 合并净利{comb/1e4:+8.1f}万 "
              f"(基线{base['net']/1e4:+.1f}万, Δ{add_y/1e4:+.1f}万)")
    if rows:
        D = pd.DataFrame(rows)
        D.to_csv(os.path.join(OUT, "q3_flip_ledger.csv"), index=False)
        print("\n  [Q3 分档明细]")
        for kx, g in D.groupby("kx"):
            tg = g[g.hit == "target"]; sp = g[g.hit == "stop"]
            print(f"   {kx}×ATR: 止盈{len(tg)}笔({len(tg)/len(g)*100:.0f}%) "
                  f"止损{len(sp)}笔 | 止盈单均{tg.Y.mean()/1e4:+.2f}万 "
                  f"止损单均{sp.Y.mean()/1e4:+.2f}万")


if __name__ == "__main__":
    q1_robust()
    q3_flip()
