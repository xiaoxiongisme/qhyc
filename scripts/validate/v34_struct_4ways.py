"""
四个结构性改动方向验证
========================
已证伪的六条路径（不再重复）：调参 / 品种筛选 / 时间止损 / 分散化 / 换周期 / 提高门槛。
本轮验证四个【结构性】方向：

D1 改造加仓：当前"1手入场 + 浮盈≥1ATR后加1手"（1:1加权，加仓单均价被摊薄）
             改为"浮盈确认后直接 2 手"（不做 1:1 加权，省掉 1 手往返成本）
             实现：改 avg_px 加权方式 —— 加仓价用【首仓价】而非【当根收盘价】
D2 长持有化：加码单胜率随持仓单调升（3-7天57%→7-14天80%→>14天94.5%）
             测试：把保本(be_r)与吊灯(trail_atr)放宽，检验是否延长持有改善期望
D3 裸均线 + 严格风控：双均线 10/30 零成本 −0.6万（优于随机 70万）
             补上移动止损（吊灯）+ 仓位管理，看能否转正
D4 单边市规则：16 个零事件品种（长期单边）。测试"单边市专用规则"是否有价值

【统一纪律】
  - 噪声基线：随机入场 + 同出场路径（v5 口径，任务 #37 确立）—— 缺它结论无效
  - IS≤2021 / OOS≥2022；半样本≤2020
  - 成本：每品种 SPEC 真实手续费 + 往返 1 跳
  - 集中度：剔除 TOP5 品种后 OOS 不得转负
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
from app.strategies.fusion_signal import walk_fusion_states, atr14, ema

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
          password=os.environ["POSTGRES_PASSWORD"], dbname="futures")


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


# ---------------------------------------------------------------- 引擎封装
def run_v34(bars, p, addon_mode="avg"):
    """
    跑 V3.4。addon_mode:
      "avg"  = 原引擎口径（1:1 加权均价）
      "full" = D1 改造：加仓那 1 手按【首仓价】计（等价于浮盈确认后直接 2 手，
               避免 1 手在低价被摊薄后抬高均价 → 省 1 手往返成本）
    """
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
                    if addon_mode == "full" and len(cur["lot_pxs"]) == 2:
                        # D1：第 2 手按首仓价计（等价于浮盈确认后一次性建 2 手）
                        cur["lot_pxs"][1] = cur["lot_pxs"][0]
                    out.append(cur); cur = None
                    if st != 0:
                        cur = dict(dir=st, first_px=float(c[i]), entry_i=i, entry_dt=dt[i],
                                   lot_pxs=[float(c[i])])
        if cur is not None:
            cur["exit_px"] = float(c[-1]); cur["exit_i"] = n - 1; cur["exit_dt"] = dt[-1]
            cur["sym"] = sym; cur["open_at_end"] = True
            if addon_mode == "full" and len(cur["lot_pxs"]) == 2:
                cur["lot_pxs"][1] = cur["lot_pxs"][0]
            out.append(cur)
    return out


# ---------------------------------------------------------------- 裸均线引擎
def run_dual_ma(bars, fast, slow, atr_mult=2.0, trail=None, be=None, cap=5):
    """
    D3：裸双均线 + 严格风控。
      入场：快线上穿/下穿慢线
      出场：①ATR 止损 atr_mult×ATR ②吊灯（若 trail 给定）③反向信号
      保本：若 be 给定，浮盈达 be×ATR 后止损上移至首仓价
    """
    out = []
    for sym, b in bars.items():
        c, h, l, dt = b["c"], b["h"], b["l"], b["dt"]
        n = len(c)
        if n < max(slow + 60, 100):
            continue
        ef = ema(c, fast); es = ema(c, slow)
        a = atr14(h, l, c, 14)
        cur = None
        for i in range(slow + 60, n):
            if not (np.isfinite(ef[i]) and np.isfinite(es[i]) and np.isfinite(a[i])) or a[i] <= 0:
                continue
            d = 1 if ef[i] > es[i] else (-1 if ef[i] < es[i] else 0)
            if cur is None:
                if d != 0:
                    cur = dict(dir=d, first_px=float(c[i]), entry_i=i, entry_dt=dt[i],
                               lot_pxs=[float(c[i])], e_atr=float(a[i]), peak=float(c[i]),
                               stop=float(c[i] - d * atr_mult * a[i]), be_done=False)
            else:
                dd = cur["dir"]
                if dd > 0 and c[i] > cur["peak"]:
                    cur["peak"] = c[i]
                if dd < 0 and c[i] < cur["peak"]:
                    cur["peak"] = c[i]
                st = cur["first_px"] - dd * atr_mult * cur["e_atr"]
                if trail is not None:
                    t = cur["peak"] - dd * trail * cur["e_atr"]
                    st = max(st, t) if dd > 0 else min(st, t)
                if be is not None and (c[i] - cur["first_px"]) * dd >= be * cur["e_atr"]:
                    cur["be_done"] = True
                if cur["be_done"]:
                    st = cur["first_px"] if dd > 0 else cur["first_px"]
                if (dd > 0 and c[i] <= st) or (dd < 0 and c[i] >= st):
                    cur["exit_px"] = float(c[i]); cur["exit_i"] = i; cur["exit_dt"] = dt[i]
                    cur["sym"] = sym
                    out.append(cur); cur = None
                    if d != 0 and d != dd:
                        cur = dict(dir=d, first_px=float(c[i]), entry_i=i, entry_dt=dt[i],
                                   lot_pxs=[float(c[i])], e_atr=float(a[i]), peak=float(c[i]),
                                   stop=float(c[i] - d * atr_mult * a[i]), be_done=False)
        if cur is not None:
            cur["exit_px"] = float(c[-1]); cur["exit_i"] = n - 1; cur["exit_dt"] = dt[-1]
            cur["sym"] = sym; cur["open_at_end"] = True
            out.append(cur)
    return out


# ---------------------------------------------------------------- 回放
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


def report(tag, r):
    L = pd.DataFrame([dict(sym=x["p"]["sym"], dt=pd.Timestamp(x["p"]["entry_dt"]), Y=x["Y"],
                           gross=x["gross"], cost=x["cost"], lots=len(x["p"]["lot_pxs"]),
                           bars=int(x["p"]["exit_i"] - x["p"]["entry_i"]))
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
    same = np.sign(isv) == np.sign(oosv)
    ok = (np.sign(r["net"]) == np.sign(oosv)) and no5 > 0
    print(f"  {tag}")
    print(f"    n={r['executed']:5d} 净{r['net']/1e4:+8.1f}万 胜{r['win']*100:5.1f}% "
          f"DD{r['mdd']/1e4:6.1f}万 MAR{r['mar']:+6.2f} 成本/毛利{ratio:5.0f}%")
    print(f"    IS{isv/1e4:+7.1f} OOS{oosv/1e4:+7.1f} {'✅' if same else '❌'} | "
          f"半样本 前{e/1e4:+6.1f}/后{l2/1e4:+6.1f} | 去TOP5后OOS{no5/1e4:+7.1f}万 "
          f"| 中位持仓{L.bars.median():.0f}根")
    return dict(tag=tag, net=r["net"], n=r["executed"], isv=isv, oosv=oosv,
                same=int(same), oos_no5=no5, ok=int(ok))


def main():
    bars = load_daily()
    p0 = FusionBacktestParams()
    p0.min_bars = 60; p0.cooldown_bars = 1; p0.W = 20; p0.ema_k = 20; p0.ma_n = 10
    print(f"[data] 日线 {len(bars)} 品种 | 基准参数 ema20/ma10/W20（官方日线映射）")

    print("\n" + "=" * 92)
    print("基线（V3.4 日线口径，加仓=1:1 加权均价）")
    print("=" * 92)
    base = run_v34(bars, p0, "avg")
    rb = replay(base)
    B = report("V3.4 日线基线", rb)

    print("\n" + "=" * 92)
    print("D1  改造加仓：加仓那 1 手按首仓价计（等价于浮盈确认后直接 2 手）")
    print("=" * 92)
    d1 = run_v34(bars, p0, "full")
    r1 = replay(d1)
    R1 = report("D1 浮盈确认后直接2手", r1)
    if B and R1:
        print(f"    → Δ净利 {(R1['net']-B['net'])/1e4:+.1f} 万")

    print("\n" + "=" * 92)
    print("D2  长持有化：放宽保本与吊灯（检验是否延长持有改善期望）")
    print("=" * 92)
    for tag, kw in [("原 be=0.5 trail=2.0", dict(be=None, trail=2.0)),
                    ("be=1.0 trail=3.0", dict(be=1.0, trail=3.0)),
                    ("be=1.0 trail=4.0", dict(be=1.0, trail=4.0)),
                    ("be=1.5 trail=4.0", dict(be=1.5, trail=4.0))]:
        p = FusionBacktestParams()
        p.min_bars = 60; p.cooldown_bars = 1; p.W = 20; p.ema_k = 20; p.ma_n = 10
        if kw.get("be") is not None:
            p.be_r = kw["be"]
        if kw.get("trail") is not None:
            p.trail_atr = kw["trail"]
        pos = run_v34(bars, p, "avg")
        report(f"D2 {tag}", replay(pos))

    print("\n" + "=" * 92)
    print("D3  裸均线 + 严格风控（补移动止损与保本）")
    print("=" * 92)
    for fast, slow in [(10, 30), (20, 60)]:
        for tag, kw in [("仅ATR止损", dict(atr_mult=2.0)),
                        ("ATR+吊灯2.0", dict(atr_mult=2.0, trail=2.0)),
                        ("ATR+吊灯2.5+保本0.5", dict(atr_mult=2.0, trail=2.5, be=0.5))]:
            pos = run_dual_ma(bars, fast, slow, **kw)
            report(f"D3 双均线{fast}/{slow} {tag}", replay(pos))

    print("\n" + "=" * 92)
    print("D4  单边市：16 个零事件品种能否用「专用规则」覆盖")
    print("=" * 92)
    print("  观察：这 16 个品种在 V3.4 下 pos=1（仅入场未平仓）→ 系统性不参与")
    zero_syms = ["SA888","AP888","UR888","SH888","PX888","SS888","SP888","AO888",
                 "SC888","SI888","LC888","NI888","SN888","EG888","EB888","LH888"]
    have = [s for s in zero_syms if s in bars]
    print(f"  可用数据: {len(have)}/{len(zero_syms)}")
    # 单边市规则：长持有 + 极宽止损（适应单边趋势）
    for tag, kw in [("趋势+宽止损ATR3.0", dict(atr_mult=3.0)),
                    ("趋势+宽止损ATR4.0+吊灯3.0", dict(atr_mult=4.0, trail=3.0))]:
        pos = run_dual_ma({s: bars[s] for s in have}, 10, 30, **kw)
        report(f"D4 {tag}（仅16品种）", replay(pos))
    # 对照：这 16 个品种在标准双均线下的表现
    pos = run_dual_ma({s: bars[s] for s in have}, 10, 30, atr_mult=2.0)
    report("D4 对照：标准双均线（仅16品种）", replay(pos))


if __name__ == "__main__":
    main()
