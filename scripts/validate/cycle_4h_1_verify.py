"""
4 小时线周期验证 —— 方向 1（成本/波动比例）
===========================================
【要验的假设】任务 #38 根因结论：在日线上单笔毛利(~50元) < 单笔成本(~68元)，
  趋势策略频繁试错必然被成本吃掉。若成立，**提高周期密度**应改善"单笔幅度/成本"比例：
  - 单笔波幅 ∝ 周期长度（4h 的波幅 ≈ 日线的 1/2）
  - 但单笔成本不变（固定手续费 + 1跳滑点）
  ⇒ 若信号质量不随周期下降，4h 的毛利/成本比应优于日线

【合成口径（关键：夜盘归属）】
  国内期货：日盘 6 根(09/10/11/13/14/15) + 夜盘 3 根(21/22/23)
  **夜盘 21:00 起属于【下一交易日】**（国内惯例），因此：
  - 4h 分组键 = 交易日（night 会话 21:00-23:59 归入下一交易日）
  - 每交易日 9 根（夜3+日6）→ 2 根 4h + 1 根 1h 尾（余数丢弃并记录）
  - 若简单按自然小时拼接，夜盘 23:00 会与次日 09:00 混在一根 → 错位，必须避免

【噪声基线（任务 #37 铁律）】随机入场 + 与策略完全相同的出场路径。
  没有它，D3 那类"漂亮结果"无法判定真伪。

【口径】成本 SPEC 真实手续费 + 往返 1 跳；容量 5 FIFO；IS≤2021 / OOS≥2022
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
from app.strategies.fusion_signal import walk_fusion_states, atr14, ema, adx14

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
IS_SPLIT = pd.Timestamp("2023-12-31")  # 4h 序列为 tz-naive 北京时间
HALF = pd.Timestamp("2020-12-31")
PG = dict(host="127.0.0.1", port=5432, user="futures",
          password="qhyc_dev_pwd_2026", dbname="futures")


def cost(sym, mult, lots=1):
    t, f = SPEC[_PROD[sym]]
    return (2 * f + 2 * 1.0 * (t * mult)) * lots


def load_hourly():
    p = os.path.join(OUT, "hourly_bars.pkl")
    if os.path.exists(p):
        with open(p, "rb") as f:
            return pickle.load(f)
    bars = {}
    for m in _MC:
        for cand in (m.product.upper(), m.product.lower()):
            sql = ("SELECT trade_datetime, open::float8, high::float8, low::float8, close::float8 "
                   "FROM fut_kline WHERE freq='hourly' AND kind='continuous' "
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


def to_4h(b):
    """
    小时线 → 4h。关键：夜盘(21:00起)归入【下一交易日】。
    做法：给每根 bar 打"交易日标签"——
      - 21:00~23:59 → 次日
      - 09:00~15:00 → 当日
    然后按 (交易日, 时段序号) 聚合 4 根为 1 根。
    每交易日 9 根 → 2 根 4h（余 1 根丢弃，记录丢弃数）。
    """
    # ★ 时区：fut_kline.trade_datetime 存的是 UTC，必须转北京时间(UTC+8)后再判断时段
    dt = pd.to_datetime(pd.Series(b["dt"]), utc=True).dt.tz_convert("Asia/Shanghai")
    dt = dt.dt.tz_localize(None)
    hhmm = dt.dt.strftime("%H:%M")
    day = dt.dt.normalize()
    # 夜盘时段：21:00~23:59 + 次日 00:00~02:59（跨午夜）→ 均归入【下一交易日】
    night = (hhmm >= "21:00") | (hhmm <= "02:59")
    trade_day = day.copy()
    trade_day[night] = (day[night] + pd.Timedelta(days=1))
    # 时段序号：夜盘(21/22/23/00/01/02) 在前，日盘(09/10/11/13/14/15) 在后
    slot = pd.Series(-1, index=dt.index, dtype=float)
    slot[hhmm == "21:00"] = 0
    slot[hhmm == "22:00"] = 1
    slot[hhmm == "23:00"] = 2
    slot[hhmm == "00:00"] = 3
    slot[hhmm == "01:00"] = 4
    slot[hhmm == "02:00"] = 5
    slot[hhmm == "09:00"] = 6
    slot[hhmm == "10:00"] = 7
    slot[hhmm == "11:00"] = 8
    slot[hhmm == "13:00"] = 9
    slot[hhmm == "14:00"] = 10
    slot[hhmm == "15:00"] = 11
    df = pd.DataFrame(dict(g=trade_day.astype(str).values, o=b["o"], h=b["h"], l=b["l"],
                           c=b["c"], t=dt.values, slot=slot.values))
    df = df[df["slot"] >= 0]
    # ★ 分组键必须含 slot//4 —— 只按交易日分组会把一天 12 根合成成 1 根
    df["g"] = df["g"] + "_" + (df["slot"] // 4).astype(int).astype(str)
    agg = (df.groupby("g", sort=False)
             .agg(o=("o", "first"), h=("h", "max"), l=("l", "min"),
                  c=("c", "last"), t=("t", "last"), n=("c", "size"))
             .reset_index(drop=True))
    # 只保留满 4 根的组（余数丢弃）
    full = agg[agg.n == 4].reset_index(drop=True)
    dropped = int((agg.n < 4).sum())
    # ★ 必须按真实时间重排：groupby(sort=False) 保持首次出现顺序，
    #   而夜盘组先于其对应日盘出现 → 序列乱序会让 walk_fusion_states 的 bar 序列全错
    full = full.sort_values("t").reset_index(drop=True)
    return dict(dt=full.t.values, o=full.o.values.astype(float), h=full.h.values.astype(float),
                l=full.l.values.astype(float), c=full.c.values.astype(float)), dropped


def run_v34(bars, p):
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


def report(tag, r, extra=""):
    L = pd.DataFrame([dict(sym=x["p"]["sym"], dt=pd.Timestamp(x["p"]["entry_dt"]), Y=x["Y"],
                           gross=x["gross"], cost=x["cost"], lots=len(x["p"]["lot_pxs"]))
                      for x in r["rows"]])
    if not len(L):
        print(f"  {tag}: 无交易")
        return None
    dcol = pd.to_datetime(L.dt)
    if getattr(dcol.dt, 'tz', None) is not None:
        dcol = dcol.dt.tz_localize(None)
    IS_S = IS_SPLIT.tz_localize(None) if IS_SPLIT.tzinfo else IS_SPLIT
    HL_S = HALF.tz_localize(None) if HALF.tzinfo else HALF
    isv = L[dcol <= IS_S].Y.sum(); oosv = L[dcol > IS_S].Y.sum()
    e = L[dcol <= HL_S].Y.sum(); l2 = L[dcol > HL_S].Y.sum()
    oosL = L[dcol > IS_S]
    t5 = oosL.groupby("sym").Y.sum().nlargest(5).index.tolist() if len(oosL) else []
    no5 = oosL[~oosL.sym.isin(t5)].Y.sum() if len(oosL) else 0.0
    ratio = 100 * L.cost.sum() / max(L.gross.sum(), 1e-9)
    print(f"  {tag} {extra}")
    print(f"    n={r['executed']:5d} 净{r['net']/1e4:+8.1f}万 胜{r['win']*100:5.1f}% "
          f"DD{r['mdd']/1e4:6.1f}万 MAR{r['mar']:+6.2f} 成本/毛利{ratio:5.0f}% "
          f"笔均毛利{L.gross.mean():+6.0f}元 笔均成本{L.cost.mean():5.0f}元")
    print(f"    IS{isv/1e4:+7.1f} OOS{oosv/1e4:+7.1f} {'✅' if np.sign(isv)==np.sign(oosv) else '❌'}"
          f" | 半样本 前{e/1e4:+6.1f}/后{l2/1e4:+6.1f} | 去TOP5后OOS{no5/1e4:+7.1f}万")
    return dict(tag=tag, net=r["net"], n=r["executed"], isv=isv, oosv=oosv,
                ratio=ratio, per_gross=L.gross.mean(), per_cost=L.cost.mean())


def random_baseline(bars, seed, atr_mult=2.0, max_hold=250):
    """噪声基线：随机入场 + 2×ATR 止损 + 持仓上限（与策略同出场路径）—— 任务 #37 铁律"""
    out = []
    for sym, b in bars.items():
        c, h, l, dt = b["c"], b["h"], b["l"], b["dt"]
        n = len(c)
        if n < 200:
            continue
        a = atr14(h, l, c, 14)
        rng = np.random.default_rng(seed)
        last = 0
        for i in range(200, n):
            if i <= last or rng.random() >= 0.02:
                continue
            if not np.isfinite(a[i]) or a[i] <= 0:
                continue
            d = 1 if rng.random() < 0.5 else -1
            stp = atr_mult * a[i]
            x_i = None; x_px = None
            for j in range(i + 1, min(i + max_hold, n)):
                if d > 0 and c[j] <= c[i] - stp:
                    x_i, x_px = j, c[i] - stp; break
                if d < 0 and c[j] >= c[i] + stp:
                    x_i, x_px = j, c[i] + stp; break
            if x_i is None:
                continue
            out.append(dict(dir=d, first_px=float(c[i]), entry_i=i, entry_dt=dt[i],
                            exit_px=float(x_px), exit_i=x_i, exit_dt=dt[x_i],
                            sym=sym, open_at_end=False, lot_pxs=[float(c[i])]))
            last = x_i
    return out


def main():
    hb = load_hourly()
    print(f"[data] 小时线 {len(hb)} 品种")

    # ---- 合成 4h
    bars4 = {}
    tot_drop = 0
    for s, b in hb.items():
        nb, drop = to_4h(b)
        tot_drop += drop
        if len(nb["c"]) >= 500:
            bars4[s] = nb
    n4 = [len(v["c"]) for v in bars4.values()]
    n1 = [len(v["c"]) for v in hb.values()]
    print(f"[4h 合成] {len(bars4)} 品种 | 4h 根数 中位 {int(np.median(n4))} "
          f"(小时线中位 {int(np.median(n1))}) | 压缩比 {np.median(n1)/np.median(n4):.1f}x "
          f"| 丢弃余数组 {tot_drop}")
    print("  口径：夜盘 21:00-23:59 归入下一交易日；每交易日 9 根 → 2 根满 4h，余 1 根丢弃")

    # ---- V3.4 on 4h（参数按周期等比缩放：日线 ema20/ma10 → 4h ema30/ma15）
    print("\n" + "=" * 90)
    print("STEP 1  V3.4 @ 4h（参数：EMA 方向层 30 / 入场 15 / W 60，与日线口径等比）")
    print("=" * 90)
    p4 = FusionBacktestParams()
    p4.ema_k = 30; p4.ma_n = 15; p4.W = 60
    p4.min_bars = 200; p4.cooldown_bars = 2
    print(f"  params: ema_k={p4.ema_k} ma_n={p4.ma_n} W={p4.W} min_bars={p4.min_bars} "
          f"| 不变 adx_min={p4.adx_min} sl/trail={p4.sl_atr}/{p4.trail_atr} fib_tol={p4.fib_tol_atr}")
    r4 = replay(run_v34(bars4, p4))
    R4 = report("V3.4 @ 4h", r4)

    print("\n" + "=" * 90)
    print("STEP 2  对照：V3.4 @ 日线（任务 #38 基线，净 −17.3 万）")
    print("=" * 90)
    db = os.path.join(OUT, "daily_bars.pkl")
    if os.path.exists(db):
        with open(db, "rb") as f:
            dbar = pickle.load(f)
        pd_ = FusionBacktestParams()
        pd_.ema_k = 20; pd_.ma_n = 10; pd_.W = 20; pd_.min_bars = 60; pd_.cooldown_bars = 1
        rd = replay(run_v34(dbar, pd_))
        RD = report("V3.4 @ 日线", rd)
    else:
        RD = None
        print("  [跳过] 无 daily_bars.pkl")

    print("\n" + "=" * 90)
    print("STEP 3  ★ 噪声基线（随机入场 + 同出场路径）@ 4h —— 任务 #37 铁律")
    print("=" * 90)
    nets = []
    for seed in range(30):
        pos = random_baseline(bars4, seed)
        rr = replay(pos)
        L = pd.DataFrame([dict(dt=pd.Timestamp(x["p"]["entry_dt"]).tz_localize(None)
                              if pd.Timestamp(x["p"]["entry_dt"]).tzinfo else pd.Timestamp(x["p"]["entry_dt"]),
                           Y=x["Y"]) for x in rr["rows"]])
        nets.append(L.Y.sum() if len(L) else 0.0)
    nets = np.array(nets)
    lo, hi = np.percentile(nets, 2.5) / 1e4, np.percentile(nets, 97.5) / 1e4
    print(f"  30 次随机 @4h: 中位{np.median(nets)/1e4:+7.1f}万 标准差{nets.std()/1e4:6.1f}万 "
          f"| 95%区间[{lo:+.0f},{hi:+.0f}]万 宽度{hi-lo:.0f}万 | 正收益{(nets>0).sum()}/30")
    print(f"  → 4h 上的噪声基准：什么都不做 ≈ {np.median(nets)/1e4:+.1f} 万")

    print("\n" + "=" * 90)
    print("STEP 4  判决")
    print("=" * 90)
    print(f"  {'口径':<16}{'成交':>7}{'净利(万)':>11}{'笔均毛利':>10}{'笔均成本':>10}{'成本/毛利':>10}{'IS':>9}{'OOS':>9}")
    for res in (R4, RD):
        if res:
            print(f"  {res['tag'][:14]:<16}{res['n']:>7}{res['net']/1e4:>11.1f}"
                  f"{res['per_gross']:>10.0f}{res['per_cost']:>10.0f}{res['ratio']:>9.0f}%"
                  f"{res['isv']/1e4:>9.1f}{res['oosv']/1e4:>9.1f}")
    print(f"  {'随机@4h(中位)':<16}{'':>7}{np.median(nets)/1e4:>11.1f}")
    print()
    if R4 and RD:
        pg4 = R4["per_gross"]; pgd = RD["per_gross"]
        print(f"  ★ 单笔毛利: 4h {pg4:+.0f}元 vs 日线 {pgd:+.0f}元 "
              f"({(pg4/pgd-1)*100:+.0f}%) ← 假设要求 4h 更大")
        print(f"  ★ 成本/毛利: 4h {R4['ratio']:.0f}% vs 日线 {RD['ratio']:.0f}%")
        if R4["net"] > 0 and R4["net"] > np.median(nets) and np.sign(R4["isv"]) == np.sign(R4["oosv"]):
            print("  ✅ 4h 上策略优于随机且 IS/OOS 同号 → 方向 1 有效")
        else:
            print("  ❌ 4h 未改善（净利未转正 / 不优于随机 / IS-OOS 翻号）→ 方向 1 否决")


if __name__ == "__main__":
    main()
