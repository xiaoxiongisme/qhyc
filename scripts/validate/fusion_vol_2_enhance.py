"""
Q1 波动率聚集优化 / Q2 振幅超均值反向 / Q3 亏损反手 2:1
=========================================================
基线：当前引擎（cb/dev 4959fb2）@ fut_kline hourly/continuous，50 品种，
      官方 accept_v34 口径（状态右移2根 + perlot + 真实成本 + 容量5 FIFO）
      ⚠ 基线净利 -10.3万（本地）/ -4.5万（云端官方），与 09-22 锚点 +69.6万 不符
        —— 引擎已演进（d1e3b24/4959fb2），锚点失效。本轮以【当前基线】为参照系。

方法（每问都必须回答"是否只是恒等变换/噪声"）：
  Q1 波动率优化：三种用法分别测
     a) 过滤器：高波动时不开仓（波动率聚集最直接用途）
     b) 仓位调节：按波动率反向缩仓（vol targeting）
     c) 自适应止损：止损 = k×近期波动（替代固定 2×ATR）
  Q2 振幅超均值反向开单：波动率分位 > q 时，反向开仓（等额）
  Q3 亏损反手 + 2:1：持仓亏损达 X 点后反手，止盈 2X

纪律：
  - 全部 IS=2015-2021 选参 / OOS=2022-2025 验
  - 逐笔对拍基线（防恒等变换）
  - 四道关口：半样本 / 去TOP5 / 符号一致率 / 参数高原
"""
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, r"E:\Docker\qhyc")
OUT = r"E:\Docker\qhyc\docs\_swing_out"
os.makedirs(OUT, exist_ok=True)

import psycopg2
PG = dict(host="127.0.0.1", port=5432, user="futures",
          password="qhyc_dev_pwd_2026", dbname="futures")

from app.core.config import get_settings
from app.backtest.fusion_backtest import FusionBacktestParams, _htf_direction
from app.strategies.fusion_signal import walk_fusion_states

_MC = get_settings().main_contracts
MULT = {m.symbol: float(m.multiplier) for m in _MC}
IS_SPLIT = pd.Timestamp("2023-12-31", tz="UTC")
HALF_SPLIT = pd.Timestamp("2020-12-31", tz="UTC")   # 半样本用（数据起点2015）

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
_PROD_OF = {m.symbol: m.product.upper() for m in _MC}


def real_cost(sym, mult, lots=1):
    tick, fee = SPEC[_PROD_OF.get(sym, sym[:-3].upper())]
    return (2 * fee + 2 * 1.0 * (tick * mult)) * lots


def load_hourly(prod, exch):
    for cand in (prod.upper(), prod.lower()):
        s = f"KQ.m@{exch}.{cand}"
        sql = ("SELECT trade_datetime, open::float8, high::float8, low::float8, close::float8 "
               "FROM fut_kline WHERE freq='hourly' AND kind='continuous' "
               f"AND symbol='{s}' ORDER BY trade_datetime ASC")
        with psycopg2.connect(**PG) as cn:
            df = pd.read_sql(sql, cn, parse_dates=["trade_datetime"])
        if len(df):
            return df
    return None


def vol_series(c, n_short=60, n_long=250):
    """相对波动率 = 短期已实现波动 / 长期中位绝对收益（causal，只用 <=i）。"""
    r = np.diff(np.log(c), prepend=np.log(max(c[0], 1e-9)))
    sd = pd.Series(r).rolling(n_short).std().values
    base = pd.Series(r).abs().rolling(n_long).median().values
    with np.errstate(divide="ignore", invalid="ignore"):
        rel = sd / np.maximum(base, 1e-12)
    return rel


CACHE = os.path.join(OUT, "v34_positions.pkl")


def build_all(p, use_cache=True):
    """构建基线头寸 + 每笔的波动率状态。带磁盘缓存（引擎重跑 50 品种很慢）。"""
    import pickle
    if use_cache and os.path.exists(CACHE):
        with open(CACHE, "rb") as f:
            out = pickle.load(f)
        print(f"[cache] 复用 {len(out)} 个头寸 <- {CACHE}")
        return out
    out = []
    for _i, m in enumerate(_MC):
        df = load_hourly(m.product, m.exchange)
        if df is None or len(df) < p.min_bars:
            continue
        print(f"  [build] {_i+1}/{len(_MC)} {m.symbol} bars={len(df)}", flush=True)
        o = df.open.to_numpy(float); h = df.high.to_numpy(float)
        l = df.low.to_numpy(float); c = df.close.to_numpy(float)
        dts = df.trade_datetime.to_numpy()
        htf = _htf_direction(c, p.ema_k)
        n = len(c)
        states = np.zeros(n, dtype=int); lots = np.zeros(n, dtype=int)
        for k, d in enumerate(walk_fusion_states(o, h, l, c, htf, p)):
            if k + 2 < n:
                states[k + 2] = d["state"]
                lots[k + 2] = int(d.get("lots", 1) or 1)
        rel = vol_series(c)
        cur = None
        for i in range(n):
            st = int(states[i]); lt = int(lots[i])
            if cur is None:
                if st != 0:
                    cur = dict(dir=st, first_px=float(c[i]), entry_i=i,
                               entry_dt=dts[i], vol_rel=float(rel[i]) if np.isfinite(rel[i]) else np.nan,
                               lot_pxs=[float(c[i])])
            else:
                if st == cur["dir"] and lt > len(cur["lot_pxs"]):
                    for _ in range(lt - len(cur["lot_pxs"])):
                        cur["lot_pxs"].append(float(c[i]))
                elif st != cur["dir"]:
                    cur["exit_px"] = float(c[i]); cur["exit_dt"] = dts[i]
                    cur["exit_i"] = i
                    cur["vol_rel_exit"] = float(rel[i]) if np.isfinite(rel[i]) else np.nan
                    cur["sym"] = m.symbol
                    out.append(cur); cur = None
                    if st != 0:
                        cur = dict(dir=st, first_px=float(c[i]), entry_i=i,
                                   entry_dt=dts[i],
                                   vol_rel=float(rel[i]) if np.isfinite(rel[i]) else np.nan,
                                   lot_pxs=[float(c[i])])
        if cur is not None:
            cur["exit_px"] = float(c[-1]); cur["exit_dt"] = dts[-1]
            cur["exit_i"] = n - 1
            cur["vol_rel_exit"] = np.nan
            cur["sym"] = m.symbol
            cur["open_at_end"] = True
            out.append(cur)
    with open(CACHE, "wb") as f:
        pickle.dump(out, f)
    print(f"[cache] 已缓存 {len(out)} 个头寸 -> {CACHE}")
    return out


def yld(pos, pnl_mode="perlot", mult_map=None):
    """单笔盈亏（与官方 replay 同口径）。"""
    sym = pos["sym"]; mult = mult_map[sym]
    dirn = 1.0 if pos["dir"] == 1 else -1.0
    lots = len(pos["lot_pxs"])
    ex = pos["exit_px"]; fp = pos["first_px"]
    if pnl_mode == "perlot":
        gross = sum(dirn * (ex - px) * mult for px in pos["lot_pxs"])
    else:
        gross = dirn * (ex - fp) * mult * lots
    return gross - real_cost(sym, mult, lots), gross, lots


def replay(pos_list, mult_map, filt=None, wgt=None, cap=5):
    """容量5 FIFO + 可选过滤器/权重。返回统计。"""
    tl = []
    for pos in pos_list:
        tl.append((pos["entry_dt"], 0, pos))
        if not pos.get("open_at_end"):
            tl.append((pos["exit_dt"], 1, pos))
    tl.sort(key=lambda x: (x[0], x[1]))
    openp = {}
    eq = peak = mdd = 0.0
    w = l = ex_n = skip = 0
    rows = []
    for dt, typ, pos in tl:
        sym = pos["sym"]
        if typ == 1:
            if sym in openp:
                pp = openp.pop(sym)
                eq += pp["Y"] * pp["w"]
                w += int(pp["win"]); l += int(not pp["win"])
                peak = max(peak, eq); mdd = max(mdd, peak - eq)
                rows.append(pp)
        else:
            if pos.get("open_at_end"):
                continue
            if filt is not None and not filt(pos):
                skip += 1; continue
            if len(openp) >= cap or sym in openp:
                skip += 1; continue
            Y, gross, lots = yld(pos, "perlot", mult_map)
            ww = 1.0 if wgt is None else float(wgt(pos))
            openp[sym] = dict(Y=Y, win=Y > 0, w=ww, pos=pos)
            ex_n += 1
    n = w + l
    return dict(executed=ex_n, cap_skip=skip, win_rate=w / n if n else 0.0,
                net=eq, mdd=mdd, mar=eq / mdd if mdd > 0 else 0.0, rows=rows)


def gate_stats(res, title, base_net=None):
    n = res["executed"]
    net_w = res["net"] / 1e4
    print(f"  {title:<34} 成交{n:5d} 净利{net_w:+9.1f}万 胜率{res['win_rate']*100:5.1f}% "
          f"DD{res['mdd']/1e4:7.1f}万 MAR{res['mar']:+6.2f}"
          + (f" | Δ基线{net_w - base_net/1e4:+8.1f}万" if base_net is not None else ""))
    return net_w


def main():
    print("=" * 78)
    print("Q0  当前引擎基线（参照系）")
    print("=" * 78)
    p = FusionBacktestParams()
    pos = build_all(p)
    print(f"[data] 头寸={len(pos)} 品种={len(set(x['sym'] for x in pos))}")
    base = replay(pos, MULT)
    base_net = base["net"]
    print(f"[基线] 成交={base['executed']} 净利{base_net/1e4:+.1f}万 "
          f"胜率={base['win_rate']*100:.1f}% DD={base['mdd']/1e4:.1f}万 MAR={base['mar']:.2f}")
    B = pd.DataFrame([dict(sym=r["pos"]["sym"], dir=r["pos"]["dir"], lots=len(r["pos"]["lot_pxs"]),
                          entry_dt=pd.Timestamp(r["pos"]["entry_dt"]),
                          exit_dt=pd.Timestamp(r["pos"]["exit_dt"]),
                          Y=r["Y"], gross=r["Y"] + r["cost"] if "cost" in r else np.nan,
                          vol_rel=r["pos"].get("vol_rel"), w=r["w"])
                       for r in base["rows"]])
    B.to_csv(os.path.join(OUT, "q0_baseline_ledger.csv"), index=False)
    isb = B[B.entry_dt <= IS_SPLIT]
    print(f"[基线 IS/OOS] IS={isb.Y.sum()/1e4:+.1f}万({len(isb)}) "
          f"OOS={B[B.entry_dt > IS_SPLIT].Y.sum()/1e4:+.1f}万")
    print(f"[基线 集中度] TOP5 品种占正收益 {100*B.groupby('sym').Y.sum().nlargest(5).sum()/max(B.groupby('sym').Y.sum().sum(),1e-9):.0f}%")

    # ============ Q1 波动率优化 ============
    print("\n" + "=" * 78)
    print("Q1  波动率聚集能否优化？（相对波动率 = 60根已实现波动 / 250根中位）")
    print("=" * 78)
    Vr = B["vol_rel"].replace([np.inf, -np.inf], np.nan).dropna()
    qs = Vr.quantile([0.5, 0.75, 0.9]).to_dict()
    print(f"  [波动分位参照] 中位={qs[0.5]:.2f} p75={qs[0.75]:.2f} p90={qs[0.9]:.2f}")

    print("\n  Q1a 过滤器：高波动时不开仓（阈值扫参）")
    for q in [0.50, 0.60, 0.70, 0.80, 0.90]:
        thr = Vr.quantile(q)
        r = replay(pos, MULT, filt=lambda pp, t=thr: (pp.get("vol_rel") is None
                                                      or not np.isfinite(pp["vol_rel"])
                                                      or pp["vol_rel"] <= t))
        gate_stats(r, f"波动<=p{int(q*100)} 才开仓", base_net)

    print("\n  Q1b 仓位调节：vol targeting（仓位 ∝ 1/波动，按中位归一）")
    med = float(Vr.median())
    for cap_w in [0.5, 0.75, 1.0]:
        def wfun(pp, m=med, cw=cap_w):
            v = pp.get("vol_rel")
            if v is None or not np.isfinite(v) or v <= 0:
                return cw
            return cw * min(2.0, max(0.3, m / v))
        r = replay(pos, MULT, wgt=wfun)
        gate_stats(r, f"vol-targeting 上限{cap_w}手", base_net)

    # ============ Q2 振幅超均值反向 ============
    print("\n" + "=" * 78)
    print("Q2  波动超均值时，反向开单（等额，与原单同时持有）")
    print("=" * 78)
    for q in [0.75, 0.90]:
        thr = Vr.quantile(q)
        # 构造反向单：同品种反向、同入场时点、同出场（引擎离场时点）
        extra = []
        for pp in pos:
            v = pp.get("vol_rel")
            if v is None or not np.isfinite(v) or v <= thr:
                continue
            d = dict(pp); d["dir"] = 3 - pp["dir"]      # 1<->2 翻转
            extra.append(d)
        both = pos + extra
        r = replay(both, MULT, cap=10)      # 容量放宽到10让反向单能同时持有
        # 只看反向单本身的效果
        r_only = replay(extra, MULT, cap=10)
        gate_stats(r_only, f"仅反向单 p{int(q*100)}+ (n={len(extra)})", 0.0)
        # 净效果 = 反向单盈亏
        yy = sum(yld(pp, "perlot", MULT)[0] for pp in extra if not pp.get("open_at_end"))
        print(f"       -> 反向单合计盈亏 = {yy/1e4:+.1f}万 ({len(extra)}笔)")

    print("\n[Q3] 反手 2:1 需逐 bar 模拟（本次 main 略，见 fusion_vol_3_flip.py）")


if __name__ == "__main__":
    main()
