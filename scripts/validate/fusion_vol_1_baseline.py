"""
V3.4 基线复现（100% 复用官方口径，不重写策略逻辑）
=====================================================
复用 scripts/accept_v34.py 的口径。踩坑记录（勿改）：
  1. 状态右移 2 根：states[k+2]=frame[k].state。不右移 = 前视偏差（虚假 89% 胜率 / 852 万）
  2. perlot 计价：各手按各自入场价。按首仓价统一计价虚高 209 万
  3. 数据源 = fut_kline hourly/continuous（生产真源），非 hourly_bar akshare
  4. 成本 = 每品种 SPEC 真实手续费 + 往返 1 跳滑点
  5. 容量 5 FIFO 组合层
本脚本新增：ledger 逐笔留痕 + IS/OOS 切分（供 Q1/Q2/Q3 增强对比）
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

from app.backtest.fusion_backtest import FusionBacktestParams, _htf_direction
from app.strategies.fusion_signal import walk_fusion_states

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
ANCHOR = dict(events=16400, executed=3226, win_rate=0.2349659,
              net=695715.0, mdd=167865.2, mar=4.1445, addons=1583)

IS_SPLIT = pd.Timestamp("2023-12-31", tz="UTC")

from app.core.config import get_settings

_MC = get_settings().main_contracts          # 权威：50 品种 exchange + multiplier
MULT = {m.symbol: float(m.multiplier) for m in _MC}
# 品种 -> 交易所（取自配置，勿硬编码：真实符号形态 CZCE/CFFEX 大写、DCE/SHFE/GFEX/INE 小写）
EXCH = {m.product: m.exchange for m in _MC}
# 配置 product -> real 品种码（含 888 命名空间的映射由 prod_of 处理）


_PROD_OF = {m.symbol: m.product.upper() for m in _MC}


def real_cost_per_lot(sym, mult):
    prod = _PROD_OF.get(sym, sym[:-3].upper() if sym.endswith("888") else sym.upper())
    tick, fee = SPEC[prod]
    return 2 * fee + 2 * 1.0 * (tick * mult)


def load_fut_hourly(prod, exch):
    """复刻 _resolve_fut_symbol：先大写再小写两种形态都试。"""
    for cand in (prod.upper(), prod.lower()):
        s = f"KQ.m@{exch}.{cand}"
        sql = ("SELECT trade_datetime, open::float8, high::float8, low::float8, close::float8 "
               "FROM fut_kline WHERE freq='hourly' AND kind='continuous' "
               f"AND symbol='{s}' ORDER BY trade_datetime ASC")
        with psycopg2.connect(**PG) as cn:
            df = pd.read_sql(sql, cn, parse_dates=["trade_datetime"])
        if len(df):
            return s, df
    return None, None


def build_positions(df, p):
    """逐字复刻 accept_v34.build_positions（状态右移 2 根）。"""
    o = df["open"].to_numpy(float); h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float); c = df["close"].to_numpy(float)
    dts = df["trade_datetime"].to_numpy()
    htf = _htf_direction(c, p.ema_k)
    n = len(c)
    states = np.zeros(n, dtype=int); lots = np.zeros(n, dtype=int)
    for k, d in enumerate(walk_fusion_states(o, h, l, c, htf, p)):
        if k + 2 < n:
            states[k + 2] = d["state"]
            lots[k + 2] = int(d.get("lots", 1) or 1)
    positions, cur = [], None
    for i in range(len(states)):
        st = int(states[i]); lt = int(lots[i])
        if cur is None:
            if st != 0:
                cur = dict(dir=st, first_px=float(c[i]), entry_i=i, entry_dt=dts[i],
                           exit_dt=None, exit_px=None, lot_pxs=[float(c[i])])
        else:
            if st == cur["dir"] and lt > len(cur["lot_pxs"]):
                for _ in range(lt - len(cur["lot_pxs"])):
                    cur["lot_pxs"].append(float(c[i]))
            elif st != cur["dir"]:
                cur["exit_px"] = float(c[i]); cur["exit_dt"] = dts[i]
                cur["exit_i"] = i
                positions.append(cur); cur = None
                if st != 0:
                    cur = dict(dir=st, first_px=float(c[i]), entry_i=i, entry_dt=dts[i],
                               exit_dt=None, exit_px=None, lot_pxs=[float(c[i])])
    if cur is not None:
        cur["exit_px"] = float(c[-1]); cur["exit_dt"] = dts[-1]
        cur["exit_i"] = n - 1
        cur["open_at_end"] = True
        positions.append(cur)
    return positions


def replay_capacity5(positions, mult_map, pnl_mode="perlot"):
    tl = []
    for pos in positions:
        tl.append((pos["entry_dt"], 0, pos))
        if not pos.get("open_at_end"):
            tl.append((pos["exit_dt"], 1, pos))
    tl.sort(key=lambda x: (x[0], x[1]))
    open_pos = {}
    eq = peak = mdd = 0.0
    wins = losses = executed = cap_skip = 0
    added_exec = total_addon_lots = 0
    ledger = []
    for dt, typ, pos in tl:
        sym = pos["sym"]
        if typ == 1:
            if sym in open_pos:
                pp = open_pos.pop(sym)
                eq += pp["Y"]
                wins += int(pp["win"]); losses += int(not pp["win"])
                peak = max(peak, eq); mdd = max(mdd, peak - eq)
        else:
            if pos.get("open_at_end"):
                continue
            lots = len(pos["lot_pxs"])
            if len(open_pos) >= 5 or sym in open_pos:
                cap_skip += 1; continue
            mult = mult_map[sym]
            dirn = 1.0 if pos["dir"] == 1 else -1.0
            first_px = pos["first_px"]; exit_px = pos["exit_px"]
            if pnl_mode == "perlot":
                gross = sum(dirn * (exit_px - px) * mult for px in pos["lot_pxs"])
            else:
                gross = dirn * (exit_px - first_px) * mult * lots
            cost = real_cost_per_lot(sym, mult) * lots
            Y = gross - cost
            win = Y > 0
            open_pos[sym] = dict(Y=Y, win=win)
            executed += 1
            if lots > 1:
                added_exec += 1; total_addon_lots += (lots - 1)
            ledger.append(dict(sym=sym, dir=int(pos["dir"]), lots=lots,
                               entry_dt=pd.Timestamp(pos["entry_dt"]),
                               exit_dt=pd.Timestamp(pos["exit_dt"]),
                               entry_px=first_px, exit_px=exit_px,
                               gross=gross, cost=cost, Y=Y, win=win,
                               entry_i=pos.get("entry_i"), exit_i=pos.get("exit_i")))
    n = wins + losses
    res = dict(events=len(positions), executed=executed, cap_skip=cap_skip,
               win_rate=wins / n if n else 0.0, net=eq, mdd=mdd,
               mar=eq / mdd if mdd > 0 else float("inf"),
               added_exec=added_exec, total_addon_lots=total_addon_lots)
    return res, ledger


def load_all_positions(p):
    """按配置 50 品种加载，复刻 _resolve_fut_symbol 大小写双形态查找。"""
    all_pos, skipped, seen = [], [], set()
    for m in _MC:
        prod, exch, sym = m.product, m.exchange, m.symbol
        if sym in seen:
            continue
        s_found, df = load_fut_hourly(prod, exch)
        if s_found is None or len(df) < p.min_bars:
            skipped.append((sym, s_found or f"KQ.m@{exch}.{prod}",
                            0 if df is None else len(df)))
            continue
        all_pos.extend([dict(x, sym=sym) for x in build_positions(df, p)])
        seen.add(sym)
    return all_pos, skipped, seen


def main():
    print("=" * 76)
    print("STEP 1  V3.4 基线复现（复用 accept_v34 官方口径）")
    print("=" * 76)
    p = FusionBacktestParams()
    print(f"[params] ema_k={p.ema_k} sl_atr={p.sl_atr} adx_min={getattr(p,'adx_min','?')} "
          f"fib={getattr(p,'fib_confl','?')} lots={getattr(p,'add_max_lots','?')}")

    all_pos, skipped, seen = load_all_positions(p)
    print(f"[data] 品种={len(seen)} 跳过={len(skipped)}: {skipped[:6]}")
    print(f"[pos ] 头寸总数={len(all_pos)}")

    res, ledger = replay_capacity5(all_pos, MULT, "perlot")
    L = pd.DataFrame(ledger)
    L.to_csv(os.path.join(OUT, "v34_official_baseline.csv"), index=False)

    print("\n" + "=" * 76)
    print(f"{'指标':<16}{'本次实测':>14}{'研发锚点':>14}{'偏差':>12}")
    print("-" * 58)
    for nm, a, b in [("信号事件", res["events"], ANCHOR["events"]),
                     ("成交(executed)", res["executed"], ANCHOR["executed"]),
                     ("胜率%", round(res["win_rate"] * 100, 2), round(ANCHOR["win_rate"] * 100, 2)),
                     ("净Y(万)", round(res["net"] / 1e4, 1), round(ANCHOR["net"] / 1e4, 1)),
                     ("最大回撤Y(万)", round(res["mdd"] / 1e4, 1), round(ANCHOR["mdd"] / 1e4, 1)),
                     ("MAR", round(res["mar"], 2), round(ANCHOR["mar"], 2)),
                     ("加码头寸", res["added_exec"], ANCHOR["addons"])]:
        print(f"{nm:<16}{a:>14}{b:>14}{a - b:>12}")

    ev_ok = abs(res["events"] - ANCHOR["events"]) < 400
    print(f"\n[判定] 事件偏差 {res['events'] - ANCHOR['events']:+d} → "
          f"{'✓ 口径对齐，可继续' if ev_ok else '✗ 偏差过大'}")

    if len(L):
        yr = L.groupby(L.entry_dt.dt.year).Y.sum()
        print("\n[基线逐年净利Y] " + "  ".join(f"{k}:{v/1e4:+.1f}万" for k, v in yr.items()))
        bys = L.groupby("sym").Y.sum().sort_values(ascending=False)
        print(f"\n[基线集中度] TOP5 品种占总净利 {100*bys.head(5).sum()/max(bys.sum(),1e-9):.0f}%")
        print(bys.head(6).map(lambda v: f"{v/1e4:+.1f}万").to_string())
        isL = L[L.entry_dt <= IS_SPLIT]
        oosL = L[L.entry_dt > IS_SPLIT]
        print(f"\n[基线 IS/OOS] IS(<=2023-12)={isL.Y.sum()/1e4:+.1f}万({len(isL)}笔) "
              f"OOS={oosL.Y.sum()/1e4:+.1f}万({len(oosL)}笔)")


if __name__ == "__main__":
    main()
