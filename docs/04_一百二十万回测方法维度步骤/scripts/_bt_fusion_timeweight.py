"""
融合策略「实盘加权回测」—— 48 品种池 / 固定 1 手 / 组合层最多 5 仓同时，
2020-2026 全样本，按「入场时段参与概率」降参与（加权参与率）。

数据源：tqsdk（库里唯一覆盖 2020-2026 全跨度的口径）。
时间权重：沿用 分时段胜率_akshare_20260915.json 推导的参与概率（跨样本外应用）。

对照：同一套信号 + 同一套 5 仓闸门，p=1（不加权）vs p=时段权重（加权）。
由于是概率闸门，跑多 seed 取均值±标准差。

用法（容器内）：
  docker exec qhyc-scheduler python /app/runtime/_bt_fusion_timeweight.py --src tqsdk
"""
from __future__ import annotations
import sys, os, json, argparse
from datetime import datetime
sys.path.insert(0, "/app")
import numpy as np
import pandas as pd
from app.core.config import get_settings
from app.core.db import session_scope
from app.strategies.fusion_signal import ema, atr14, drop_forming_bars
from app.models import HourlyBar
from sqlalchemy import select

# ---- 入场时段参与概率（由 分时段胜率_akshare_20260915.json 推导，跨样本外）----
# mean_R: 23:00 -0.102(最差) ~ 11:15 +0.164(最好)。映射 p = 0.30 + 0.70 * (edge-min)/(max-min)
AKSHARE_BUCKET = {
    "10:00": -0.0474, "11:15": 0.1643, "14:15": 0.1471, "15:00": 0.0898,
    "22:00": 0.0798, "23:00": -0.1016, "00:00": -0.0725, "01:00": -0.4733,
}
_e_min, _e_max = min(AKSHARE_BUCKET.values()), max(AKSHARE_BUCKET.values())
def akshare_weight(bucket: str) -> float:
    if bucket not in AKSHARE_BUCKET:
        return 1.0  # 样本不足/罕见时段：中性（满参与）
    e = AKSHARE_BUCKET[bucket]
    p = 0.30 + 0.70 * (e - _e_min) / (_e_max - _e_min)
    return round(max(0.30, min(1.0, p)), 3)

# 注：akshare 与 tqsdk 的小时线收盘时刻不同（akshare 11:15/14:15/15:00/23:00，
# tqsdk 为整点 09:00/10:00/11:00/13:00/14:00/21:00/22:00/00:00），不能直接套用。
# 实际权重在 main() 中由本数据集(tqsdk)分时段期望值推导，akshare 仅作参照对照。


def bt_symbol(o, h, l, c, htf_dir, p):
    """逐位复刻 fusion_state_detail 前向循环，记录成交。返回 trades 列表。"""
    n = len(c)
    if n < 40:
        return []
    ma20 = ema(c, p.ma_n)
    atr_arr = atr14(h, l, c, p.atr_n)
    ph, pl = [], []
    state = 0; entry_px = 0.0; entry_i = -1; entry_dir = 0; cooldown = 0
    e_atr = 0.0; peak = 0.0; trough = 0.0; be_done = False
    trades = []
    for i in range(2, n):
        if h[i - 1] > h[i - 2] and h[i - 1] > h[i]:
            ph.append(i - 1)
        if l[i - 1] < l[i - 2] and l[i - 1] < l[i]:
            pl.append(i - 1)
        while ph and ph[0] < i - p.W:
            ph.pop(0)
        while pl and pl[0] < i - p.W:
            pl.pop(0)
        if cooldown > 0:
            cooldown -= 1
        if state == 1:
            if c[i] > peak: peak = c[i]
            st = entry_px - p.sl_atr * e_atr
            if p.trail_atr > 0:
                t = peak - p.trail_atr * e_atr
                if t > st: st = t
            if p.be_r > 0 and (c[i] - entry_px) >= p.be_r * e_atr: be_done = True
            if be_done and entry_px > st: st = entry_px
            if c[i] <= st:
                R = (c[i] - entry_px) / e_atr if e_atr > 0 else 0.0
                trades.append((entry_i, entry_px, e_atr, entry_dir, i, float(c[i]), R))
                state = 0; cooldown = p.cooldown_bars; be_done = False
        elif state == 2:
            if c[i] < trough: trough = c[i]
            st = entry_px + p.sl_atr * e_atr
            if p.trail_atr > 0:
                t = trough + p.trail_atr * e_atr
                if t < st: st = t
            if p.be_r > 0 and (entry_px - c[i]) >= p.be_r * e_atr: be_done = True
            if be_done and entry_px < st: st = entry_px
            if c[i] >= st:
                R = (entry_px - c[i]) / e_atr if e_atr > 0 else 0.0
                trades.append((entry_i, entry_px, e_atr, entry_dir, i, float(c[i]), R))
                state = 0; cooldown = p.cooldown_bars; be_done = False
        if state == 0 and cooldown == 0 and i >= 30:
            up = c[i] > ma20[i]; dn = c[i] < ma20[i]
            cross_up = c[i] > ma20[i] and c[i - 1] < ma20[i - 1]
            cross_down = c[i] < ma20[i] and c[i - 1] > ma20[i - 1]
            tl = l[i] <= ma20[i]
            sbull = c[i] > o[i] and c[i] >= 0.5 * (h[i] + l[i])
            ts = h[i] >= ma20[i]
            sbear = c[i] < o[i] and c[i] <= 0.5 * (h[i] + l[i])
            use_pb = p.entry_mode in ("pullback", "both", "both_nm")
            use_bk = p.entry_mode in ("breakout", "breakout_nomacd", "both", "both_nm")
            no_macd = p.entry_mode in ("breakout_nomacd", "both_nm")
            s_tl = use_pb and htf_dir[i] >= 0 and up and tl and sbull and cross_up
            s_ts = use_pb and htf_dir[i] <= 0 and dn and ts and sbear and cross_down
            _hh10 = max(h[max(0, i - 10):i]); _ll10 = min(l[max(0, i - 10):i])
            s_bkl = use_bk and htf_dir[i] >= 0 and c[i] > _hh10 and c[i] > o[i] and no_macd
            s_bks = use_bk and htf_dir[i] <= 0 and c[i] < _ll10 and c[i] < o[i] and no_macd
            if s_tl or s_bkl:
                state = 1; entry_px = c[i]; entry_i = i; e_atr = atr_arr[i]
                peak = c[i]; trough = c[i]; be_done = False; entry_dir = 1
            elif s_ts or s_bks:
                state = 2; entry_px = c[i]; entry_i = i; e_atr = atr_arr[i]
                peak = c[i]; trough = c[i]; be_done = False; entry_dir = 2
    if state != 0:
        trades.append((entry_i, entry_px, e_atr, entry_dir, -1, float(c[-1]), None))
    return trades


def read_src(session, symbol, limit, src):
    rows = session.execute(
        select(HourlyBar.trade_datetime, HourlyBar.open, HourlyBar.high,
               HourlyBar.low, HourlyBar.close)
        .where(HourlyBar.symbol == symbol).where(HourlyBar.src == src)
        .order_by(HourlyBar.trade_datetime.desc()).limit(limit)
    ).all()
    rows = list(reversed(rows))
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["dt", "open", "high", "low", "close"])
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    return df


def replay(events, weights, seed, max_pos=5, weighted=True):
    """组合层重放。events: list of dict(sym,entry_dt,exit_dt,bucket,R,entry_atr,mult,dir,open_at_end)。
    返回统计字典。"""
    rng = np.random.default_rng(seed)
    # 时间轴事件
    tl = []
    for e in events:
        tl.append((e["entry_dt"], 0, e))               # 0=入场（后处理）
        if not e["open_at_end"]:
            tl.append((e["exit_dt"], 1, e))            # 1=离场（先处理）
    tl.sort(key=lambda x: (x[0], x[1]), reverse=False)

    open_pos = {}   # sym -> dict(R, Y, entry_atr, mult)
    eq_R = 0.0; eq_Y = 0.0
    peak_eq_R = 0.0; mdd_R = 0.0
    peak_eq_Y = 0.0; mdd_Y = 0.0
    wins = 0; losses = 0; pf_win = 0.0; pf_loss = 0.0
    executed = 0; weight_skip = 0; cap_skip = 0
    bucket_part = {}; bucket_exec = {}; bucket_skipw = {}
    max_conc = 0; peak_risk_Y = 0.0
    per_year_R = {}; per_year_Y = {}; per_year_n = {}

    for dt, typ, e in tl:
        if typ == 1:  # 离场
            sym = e["sym"]
            if sym in open_pos:
                pos = open_pos.pop(sym)
                R = pos["R"]; Y = pos["Y"]
                eq_R += R; eq_Y += Y
                if R > 0: wins += 1; pf_win += R
                else: losses += 1; pf_loss += -R
                if eq_R > peak_eq_R: peak_eq_R = eq_R
                if eq_Y > peak_eq_Y: peak_eq_Y = eq_Y
                mdd_R = max(mdd_R, peak_eq_R - eq_R)
                mdd_Y = max(mdd_Y, peak_eq_Y - eq_Y)
                y = dt.year
                per_year_R[y] = per_year_R.get(y, 0.0) + R
                per_year_Y[y] = per_year_Y.get(y, 0.0) + Y
                per_year_n[y] = per_year_n.get(y, 0) + 1
        else:  # 入场
            sym = e["sym"]; bucket = e["bucket"]
            if e["open_at_end"]:
                continue  # 期末未平：本回测仅统计已平仓成交
            bucket_part[bucket] = bucket_part.get(bucket, 0) + 1
            participate = True
            if weighted:
                p_part = weights.get(bucket, 1.0)
                if rng.random() >= p_part:
                    participate = False
            if not participate:
                weight_skip += 1
                bucket_skipw[bucket] = bucket_skipw.get(bucket, 0) + 1
                continue
            if len(open_pos) >= max_pos or sym in open_pos:
                cap_skip += 1
                continue
            # 开仓
            R = e["R"]; entry_atr = e["entry_atr"]; mult = e["mult"]
            Y = R * entry_atr * mult
            open_pos[sym] = {"R": R, "Y": Y, "entry_atr": entry_atr, "mult": mult}
            executed += 1
            bucket_exec[bucket] = bucket_exec.get(bucket, 0) + 1
            # 资本占用（初始风险 = 2*ATR*mult）
            risk = 2.0 * entry_atr * mult
            conc = len(open_pos)
            max_conc = max(max_conc, conc)
            cur_risk = sum(2.0 * p["entry_atr"] * p["mult"] for p in open_pos.values())
            peak_risk_Y = max(peak_risk_Y, cur_risk)

    n = wins + losses
    pf = (pf_win / pf_loss) if pf_loss > 0 else (float("inf") if pf_win > 0 else 0.0)
    wr = wins / n if n else 0.0
    return {
        "executed": executed, "weight_skip": weight_skip, "cap_skip": cap_skip,
        "wins": wins, "losses": losses, "win_rate": wr,
        "pf": pf, "net_R": eq_R, "net_Y": eq_Y,
        "mdd_R": mdd_R, "mdd_Y": mdd_Y,
        "max_concurrent": max_conc, "peak_risk_Y": peak_risk_Y,
        "bucket_part": bucket_part, "bucket_exec": bucket_exec, "bucket_skipw": bucket_skipw,
        "per_year_R": per_year_R, "per_year_Y": per_year_Y, "per_year_n": per_year_n,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="tqsdk", choices=["tqsdk", "akshare", "csv"])
    ap.add_argument("--limit", type=int, default=300000)
    ap.add_argument("--max-pos", type=int, default=5)
    ap.add_argument("--seeds", type=int, default=30)
    ap.add_argument("--ref-seed", type=int, default=42)
    args = ap.parse_args()

    st = get_settings(); p = st.fusion; specs = st.main_contracts; src = args.src
    print(f"# 实盘加权回测 src={src} max_pos={args.max_pos} seeds={args.seeds}")

    events = []
    bucket_signal_stats = {}   # 信号层分时段(全样本,未闸门): n, Rsum
    skipped = 0
    for spec in specs:
        sym = spec.symbol; mult = float(spec.multiplier)
        with session_scope() as s:
            df = read_src(s, sym, args.limit, src)
        if df is None:
            skipped += 1; continue
        if p.closed_bars_only:
            df = drop_forming_bars(df)
        if len(df) < p.min_bars:
            skipped += 1; continue
        o = df["open"].to_numpy(float); h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float); c = df["close"].to_numpy(float)
        ema140 = ema(c, p.ema_k); dir_raw = np.where(c > ema140, 1, -1)
        htf_dir = np.concatenate([[0], dir_raw[:-1]])
        tr = bt_symbol(o, h, l, c, htf_dir, p)
        dts = df["dt"].tolist()
        for (ei, epx, eatr, edir, xi, xpx, R) in tr:
            if xi < 0:
                # 期末未平：仍记录入场事件，但无离场
                entry_dt = dts[ei]
                bucket = entry_dt.strftime("%H:%M")
                events.append({"sym": sym, "entry_dt": entry_dt, "exit_dt": None,
                              "bucket": bucket, "R": None, "entry_atr": eatr,
                              "mult": mult, "dir": edir, "open_at_end": True})
                bucket_signal_stats.setdefault(bucket, [0, 0.0])
                bucket_signal_stats[bucket][0] += 1
                continue
            entry_dt = dts[ei]; exit_dt = dts[xi]
            bucket = entry_dt.strftime("%H:%M")
            events.append({"sym": sym, "entry_dt": entry_dt, "exit_dt": exit_dt,
                          "bucket": bucket, "R": R, "entry_atr": eatr,
                          "mult": mult, "dir": edir, "open_at_end": False})
            bucket_signal_stats.setdefault(bucket, [0, 0.0])
            bucket_signal_stats[bucket][0] += 1
            bucket_signal_stats[bucket][1] += (R if R is not None else 0.0)

    print(f"信号层总事件(含未平)= {len(events)}  跳过品种={skipped}")

    # 由本数据集(tqsdk)分时段期望值推导参与概率 p∈[0.30,1.00]
    N_MIN = 100
    valid = {b: (r[1] / r[0]) for b, r in bucket_signal_stats.items() if r[0] >= N_MIN}
    _mn, _mx = (min(valid.values()), max(valid.values())) if valid else (0.0, 1.0)
    WEIGHTS = {}
    for b, r in bucket_signal_stats.items():
        mr = (r[1] / r[0]) if r[0] else 0.0
        if r[0] >= N_MIN and _mx > _mn:
            p = 0.30 + 0.70 * (mr - _mn) / (_mx - _mn)
            WEIGHTS[b] = round(max(0.30, min(1.0, p)), 3)
        else:
            WEIGHTS[b] = 1.0  # 样本不足：中性（满参与）

    # 信号层分时段(用于报告对照)
    bk_rows = []
    for b in sorted(bucket_signal_stats):
        n, rsum = bucket_signal_stats[b]
        bk_rows.append({"bucket": b, "n": n, "mean_R": (rsum / n if n else 0.0),
                        "weight": WEIGHTS.get(b, 1.0),
                        "weight_akshare": akshare_weight(b)})
    print("\n信号层分时段(本数据集 tqsdk) + 推导参与概率:")
    for r in bk_rows:
        print(f"  {r['bucket']:7} n={r['n']:6} meanR={r['mean_R']:+.4f}  "
              f"p(tqsdk)={r['weight']}  p(akshare参照)={r['weight_akshare']}")

    # 跑多 seed：加权 vs 不加权
    seeds = list(range(args.seeds))
    w_stats = []; u_stats = []
    for sd in seeds:
        w_stats.append(replay(events, WEIGHTS, sd, args.max_pos, weighted=True))
        u_stats.append(replay(events, WEIGHTS, sd, args.max_pos, weighted=False))

    def agg(stats, key):
        vals = [s[key] for s in stats]
        return float(np.mean(vals)), float(np.std(vals))

    w_netR = [s["net_R"] for s in w_stats]
    u_netR_val = u_stats[0]["net_R"]
    better = sum(1 for x in w_netR if x > u_netR_val)
    positive = sum(1 for x in w_netR if x > 0)
    print(f"\n===== 对照 (max_pos={args.max_pos}, {args.seeds} seeds) =====")
    for label, stt in (("不加权(p=1)", u_stats), ("加权(时段p)", w_stats)):
        netR_m, netR_s = agg(stt, "net_R")
        netY_m, netY_s = agg(stt, "net_Y")
        pf_m, pf_s = agg(stt, "pf"); pf_m = min(pf_m, 99)
        wr_m, wr_s = agg(stt, "win_rate")
        ex_m, ex_s = agg(stt, "executed")
        dd_m, dd_s = agg(stt, "mdd_R")
        print(f"[{label}] 执行={ex_m:.0f}±{ex_s:.0f}  净R={netR_m:+.1f}±{netR_s:.1f}  "
              f"净¥={netY_m/10000:+.1f}万±{netY_s/10000:.1f}  PF={pf_m:.2f}  "
              f"胜率={wr_m*100:.1f}%  maxDD(R)={dd_m:.1f}")
    print(f"  加权 netR 分布: min={min(w_netR):.1f}  max={max(w_netR):.1f}  "
          f"中位数={float(np.median(w_netR)):.1f}")
    print(f"  加权优于不加权: {better}/{args.seeds} seeds；加权为正: {positive}/{args.seeds}")

    # 参考 seed 详细
    ref_w = replay(events, WEIGHTS, args.ref_seed, args.max_pos, weighted=True)
    print(f"\n--- 加权 参考seed={args.ref_seed} 明细 ---")
    print(f"  执行={ref_w['executed']} 权重跳过={ref_w['weight_skip']} 容量跳过={ref_w['cap_skip']}")
    print(f"  净R={ref_w['net_R']:+.1f}  净¥={ref_w['net_Y']/10000:+.1f}万  PF={ref_w['pf']:.2f}  "
          f"胜率={ref_w['win_rate']*100:.1f}%  maxDD(R)={ref_w['mdd_R']:.1f} maxDD(¥)={ref_w['mdd_Y']/10000:.1f}万")
    print(f"  最大同时持仓={ref_w['max_concurrent']}  峰值占用资本(初始风险)≈{ref_w['peak_risk_Y']/10000:.1f}万")
    print("  分年度(加权参考seed):")
    for y in sorted(ref_w["per_year_Y"]):
        print(f"    {y}: ¥={ref_w['per_year_Y'][y]/10000:+.1f}万  R={ref_w['per_year_R'][y]:+.1f}  笔={ref_w['per_year_n'][y]}")

    def mean_dict(dicts, key):
        keys = set()
        for d in dicts:
            keys |= set(d[key].keys())
        return {k: float(np.mean([d[key].get(k, 0.0) for d in dicts])) for k in sorted(keys, key=str)}

    mean_py_Y = mean_dict(w_stats, "per_year_Y")
    mean_py_R = mean_dict(w_stats, "per_year_R")
    mean_py_n = mean_dict(w_stats, "per_year_n")
    mean_bexec = mean_dict(w_stats, "bucket_exec")
    mean_bskipw = mean_dict(w_stats, "bucket_skipw")

    print("\n跨 seed 平均 分年度(加权):")
    for y in mean_py_Y:
        print(f"  {y}: ¥={mean_py_Y[y]/10000:+.1f}万  R={mean_py_R[y]:+.1f}  笔={mean_py_n[y]:.0f}")

    # 落盘
    out = {
        "src": src, "max_pos": args.max_pos, "seeds": args.seeds, "ref_seed": args.ref_seed,
        "weights": WEIGHTS,
        "signal_layer_by_bucket": bk_rows,
        "unweighted": {k: (float(np.mean([s[k] for s in u_stats])) if not isinstance(u_stats[0][k], dict) else None)
                       for k in ("net_R","net_Y","pf","win_rate","executed","mdd_R","mdd_Y","max_concurrent","peak_risk_Y")},
        "weighted": {k: (float(np.mean([s[k] for s in w_stats])) if not isinstance(w_stats[0][k], dict) else None)
                     for k in ("net_R","net_Y","pf","win_rate","executed","mdd_R","mdd_Y","max_concurrent","peak_risk_Y")},
        "weighted_dist": {"min": float(min(w_netR)), "max": float(max(w_netR)),
                          "median": float(np.median(w_netR)),
                          "beats_unweighted": better, "positive": positive},
        "mean_per_year_Y": mean_py_Y, "mean_per_year_R": mean_py_R, "mean_per_year_n": mean_py_n,
        "mean_bucket_exec": mean_bexec, "mean_bucket_skipw": mean_bskipw,
        "reference_weighted": {k: ref_w[k] for k in ("executed","weight_skip","cap_skip","wins","losses",
                            "win_rate","pf","net_R","net_Y","mdd_R","mdd_Y","max_concurrent","peak_risk_Y",
                            "per_year_R","per_year_Y","per_year_n","bucket_part","bucket_exec","bucket_skipw")},
    }
    os.makedirs("/app/runtime/bt_out", exist_ok=True)
    fn = f"/app/runtime/bt_out/fusion_bt_timeweight_{src}.json"
    with open(fn, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n已落盘: {fn}")


if __name__ == "__main__":
    main()
