"""
融合策略「多方法盈利金额对照」—— 复刻原 48 品种回测的方法学（最多 5 仓 / 1 手 / 5bp / 组合层 FIFO），
用当前 DB 里可用的口径（tqsdk/akshare/csv）跑多方法变体，对照原 +120.9 万结论。

变体维度：
  1) 数据源 src：tqsdk(唯一覆盖2020起) / akshare(仅2025-26) / csv(仅FG/SA)
  2) 容量 max_pos：1/2/3/5 仓
  3) 成本 cost_bp：5/10/20/30/50
  4) 时段权重：不加权(p=1) / 加权(tqsdk 自身分时段期望推导)

用法（容器内）：
  docker exec qhyc-scheduler python /app/runtime/_bt_multi.py --src-list tqsdk,akshare,csv --seeds 60
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

# ---- 入场时段参与概率：先由本数据集分时段期望值推导（见 main）----
N_MIN = 100


def bt_symbol(o, h, l, c, htf_dir, p):
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
    if src == "fdf":
        return read_src_fdf(session, symbol, limit, _FDF_KIND)
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


# fut_kline.hourly 的 symbol 形如 KQ.m@CZCE.FG；回测品种集用 FG888 风格。
# 启动时构建 品种代码 -> KQ.m@... 的反向映射，避免硬编码交易所。
import re as _re
_FDF_KIND = "continuous"


def _build_fdf_map(session):
    from sqlalchemy import text
    rs = session.execute(text(
        "SELECT DISTINCT symbol FROM fut_kline WHERE freq='hourly'")).fetchall()
    m = {}
    for (s,) in rs:
        if s.startswith("KQ.m@"):
            prod = s.split(".", 1)[1].split(".", 1)[1]  # KQ.m@CZCE.FG -> FG
            m[prod.lower()] = s                          # 统一小写键
    return m


_FDF_MAP = None


def read_src_fdf(session, symbol, limit, kind):
    global _FDF_MAP
    if _FDF_MAP is None:
        _FDF_MAP = _build_fdf_map(session)
    prod = _re.sub(r"888$", "", symbol).lower()   # FG888 -> fg
    fq = _FDF_MAP.get(prod)
    if not fq:
        return None
    from sqlalchemy import text
    rs = session.execute(text(
        "SELECT trade_datetime, open, high, low, close FROM fut_kline "
        "WHERE freq='hourly' AND kind=:k AND symbol=:s ORDER BY trade_datetime DESC LIMIT :lim"
    ), {"k": kind, "s": fq, "lim": limit}).fetchall()
    rs = list(reversed(rs))
    if not rs:
        return None
    df = pd.DataFrame(rs, columns=["dt", "open", "high", "low", "close"])
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    return df


def replay(events, weights, seed, max_pos=5, weighted=True, cost_bp=5.0):
    rng = np.random.default_rng(seed)
    tl = []
    for e in events:
        tl.append((e["entry_dt"], 0, e))
        if not e["open_at_end"]:
            tl.append((e["exit_dt"], 1, e))
    tl.sort(key=lambda x: (x[0], x[1]), reverse=False)
    open_pos = {}
    eq_R = 0.0; eq_Y = 0.0
    peak_R = 0.0; mdd_R = 0.0
    peak_Y = 0.0; mdd_Y = 0.0
    wins = 0; losses = 0; pf_win = 0.0; pf_loss = 0.0
    executed = 0; weight_skip = 0; cap_skip = 0
    max_conc = 0; peak_risk_Y = 0.0
    per_year_Y = {}
    for dt, typ, e in tl:
        if typ == 1:
            sym = e["sym"]
            if sym in open_pos:
                pos = open_pos.pop(sym)
                R = pos["R"]; Y = pos["Y"]
                eq_R += R; eq_Y += Y
                if R > 0: wins += 1; pf_win += R
                else: losses += 1; pf_loss += -R
                if eq_R > peak_R: peak_R = eq_R
                if eq_Y > peak_Y: peak_Y = eq_Y
                mdd_R = max(mdd_R, peak_R - eq_R)
                mdd_Y = max(mdd_Y, peak_Y - eq_Y)
                per_year_Y[dt.year] = per_year_Y.get(dt.year, 0.0) + Y
        else:
            sym = e["sym"]; bucket = e["bucket"]
            if e["open_at_end"]:
                continue
            participate = True
            if weighted:
                p_part = weights.get(bucket, 1.0)
                if rng.random() >= p_part:
                    participate = False
            if not participate:
                weight_skip += 1; continue
            if len(open_pos) >= max_pos or sym in open_pos:
                cap_skip += 1; continue
            R = e["R"]; entry_atr = e["entry_atr"]; mult = e["mult"]; epx = e["entry_px"]
            # 金额 = R×ATR×乘数 − 往返成本(bp)
            cost = cost_bp / 10000.0 * epx * mult
            Y = R * entry_atr * mult - cost
            open_pos[sym] = {"R": R, "Y": Y, "entry_atr": entry_atr, "mult": mult}
            executed += 1
            conc = len(open_pos)
            max_conc = max(max_conc, conc)
            cur_risk = sum(2.0 * pp["entry_atr"] * pp["mult"] for pp in open_pos.values())
            peak_risk_Y = max(peak_risk_Y, cur_risk)
    n = wins + losses
    pf = (pf_win / pf_loss) if pf_loss > 0 else (float("inf") if pf_win > 0 else 0.0)
    wr = wins / n if n else 0.0
    return {"executed": executed, "weight_skip": weight_skip, "cap_skip": cap_skip,
            "wins": wins, "losses": losses, "win_rate": wr, "pf": pf,
            "net_R": eq_R, "net_Y": eq_Y, "mdd_R": mdd_R, "mdd_Y": mdd_Y,
            "max_concurrent": max_conc, "peak_risk_Y": peak_risk_Y,
            "per_year_Y": per_year_Y}


def build_events(src, limit, min_year=0, max_pos_filter=50):
    st = get_settings(); p = st.fusion; specs = st.main_contracts[:max_pos_filter]
    events = []; skipped = 0; sym_min_year = {}
    for spec in specs:
        sym = spec.symbol; mult = float(spec.multiplier)
        with session_scope() as s:
            df = read_src(s, sym, limit, src)
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
        sym_min_year[sym] = dts[0].year
        for (ei, epx, eatr, edir, xi, xpx, R) in tr:
            if xi < 0:
                entry_dt = dts[ei]; bucket = entry_dt.strftime("%H:%M")
                events.append({"sym": sym, "entry_dt": entry_dt, "exit_dt": None,
                              "bucket": bucket, "R": None, "entry_atr": eatr,
                              "mult": mult, "entry_px": epx, "dir": edir, "open_at_end": True})
                continue
            entry_dt = dts[ei]; exit_dt = dts[xi]; bucket = entry_dt.strftime("%H:%M")
            events.append({"sym": sym, "entry_dt": entry_dt, "exit_dt": exit_dt,
                          "bucket": bucket, "R": R, "entry_atr": eatr,
                          "mult": mult, "entry_px": epx, "dir": edir, "open_at_end": False})
    return events, skipped, sym_min_year


def derive_weights(events):
    stats = {}
    for e in events:
        if e["open_at_end"]:
            continue
        b = e["bucket"]; stats.setdefault(b, [0, 0.0]); stats[b][0] += 1
        stats[b][1] += (e["R"] if e["R"] is not None else 0.0)
    valid = {b: (r[1]/r[0]) for b, r in stats.items() if r[0] >= N_MIN}
    _mn, _mx = (min(valid.values()), max(valid.values())) if valid else (0.0, 1.0)
    W = {}
    for b, r in stats.items():
        mr = (r[1]/r[0]) if r[0] else 0.0
        if r[0] >= N_MIN and _mx > _mn:
            pp = 0.30 + 0.70 * (mr - _mn)/(_mx - _mn)
            W[b] = round(max(0.30, min(1.0, pp)), 3)
        else:
            W[b] = 1.0
    return W, stats


def fmt_y(x):
    return f"{x/10000:+.1f}万"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-list", default="tqsdk,akshare,csv")
    ap.add_argument("--limit", type=int, default=300000)
    ap.add_argument("--seeds", type=int, default=60)
    ap.add_argument("--max-pos-list", default="1,2,3,5")
    ap.add_argument("--cost-list", default="5,10,20,30,50")
    ap.add_argument("--min-year", type=int, default=0, help="只保留起始年<=该年的品种(干净窗口)")
    ap.add_argument("--tag", default="", help="输出文件名后缀，避免覆盖")
    ap.add_argument("--fdf-kind", default="continuous", choices=["continuous", "cont_adj"],
                    help="当 src 含 'fdf' 时，读取 fut_kline.hourly 的该维度")
    args = ap.parse_args()
    global _FDF_KIND
    _FDF_KIND = args.fdf_kind
    srcs = [x.strip() for x in args.src_list.split(",") if x.strip()]
    maxposs = [int(x) for x in args.max_pos_list.split(",")]
    costs = [float(x) for x in args.cost_list.split(",")]
    ref_seed = 42

    report = {"generated": datetime.now().isoformat(), "original_ref": 1209197,
              "srcs": {}}
    print("=" * 78)
    print(f"多方法盈利金额对照  (原48品种回测结论=+1,209,197元, 容量5, 5bp)")
    print(f"min_year(干净窗口)={args.min_year or '无'}  seeds={args.seeds}")
    print("=" * 78)

    for src in srcs:
        print(f"\n########## 数据源 = {src} ##########")
        events, skipped, sym_min = build_events(src, args.limit, args.min_year)
        # 干净窗口过滤
        if args.min_year:
            wl = set(s for s, y in sym_min.items() if y <= args.min_year)
            events = [e for e in events if e["sym"] in wl]
            print(f"  干净窗口(起始<={args.min_year})品种: {len(wl)} -> {','.join(sorted(wl))}")
        closed = [e for e in events if not e["open_at_end"]]
        print(f"  信号层事件(含未平)={len(events)}  已平仓={len(closed)}  跳过品种={skipped}")
        if not closed:
            print("  !! 无已平仓事件，跳过")
            continue
        W, stats = derive_weights(events)
        print(f"  时段权重(由本数据集推导): {W}")

        # ---- 表1: 容量 × 成本 (不加权, p=1) ----
        print("\n  [表1] 容量 × 成本 → 净¥ (不加权, 确定性)")
        header = "  max_pos | " + " | ".join(f"{int(c)}bp" for c in costs)
        print(header)
        tbl = {}
        for mp in maxposs:
            row = []
            for cst in costs:
                st0 = replay(events, W, 0, mp, weighted=False, cost_bp=cst)
                row.append(fmt_y(st0["net_Y"]))
                tbl[(mp, cst)] = st0
            print(f"  {mp:7} | " + " | ".join(f"{v:>10}" for v in row))

        # ---- 表2: 容量5 × 5bp 不加权 vs 加权(±seed) ----
        print("\n  [表2] 容量5 × 5bp: 不加权 vs 加权(±seed)")
        mp = 5; cst = 5.0
        u = tbl[(mp, cst)]
        w_stats = [replay(events, W, sd, mp, weighted=True, cost_bp=cst) for sd in range(args.seeds)]
        wY = [s["net_Y"] for s in w_stats]
        wR = [s["net_R"] for s in w_stats]
        wPF = [min(s["pf"], 99) for s in w_stats if s["pf"] != float("inf")]
        wWR = [s["win_rate"] for s in w_stats]
        wDD = [s["mdd_Y"] for s in w_stats]
        import numpy as np
        def m(v): return float(np.mean(v)); d = lambda v: float(np.std(v))
        print(f"  不加权: 净¥={fmt_y(u['net_Y'])}  PF={min(u['pf'],99):.2f}  胜率={u['win_rate']*100:.1f}%  "
              f"成交={u['executed']}  maxDD¥={fmt_y(u['mdd_Y'])}")
        print(f"  加权  : 净¥={fmt_y(m(wY))}±{abs(m(wY)-np.median(wY))/1e4:.1f}万   "
              f"PF={m(wPF):.2f}  胜率={m(wWR)*100:.1f}%  成交={m([s['executed'] for s in w_stats]):.0f}  "
              f"maxDD¥={fmt_y(m(wDD))}")
        print(f"         加权净¥分布: min={fmt_y(min(wY))}  中位={fmt_y(float(np.median(wY)))}  max={fmt_y(max(wY))}  "
              f"为正={(sum(1 for x in wY if x>0))}/{args.seeds} 优于不加权={sum(1 for x in wY if x>u['net_Y'])}/{args.seeds}")

        # ---- 表3: 容量扫描(5bp, 不加权) 含关键指标 ----
        print("\n  [表3] 容量扫描 @5bp 不加权: 净¥ / PF / 胜率 / 成交 / maxDD¥")
        for mp in maxposs:
            s = tbl[(mp, 5.0)]
            print(f"  {mp}仓: 净¥={fmt_y(s['net_Y'])}  PF={min(s['pf'],99):.2f}  胜率={s['win_rate']*100:.1f}%  "
                  f"成交={s['executed']}  maxDD¥={fmt_y(s['mdd_Y'])}  峰值占用≈{s['peak_risk_Y']/10000:.1f}万")

        report["srcs"][src] = {
            "events": len(events), "closed": len(closed), "skipped": skipped,
            "weights": W, "table1_netY": {f"{mp}_{cst}": tbl[(mp,cst)]["net_Y"] for mp in maxposs for cst in costs},
            "tbl3": {mp: {k: tbl[(mp,5.0)][k] for k in ("net_Y","pf","win_rate","executed","mdd_Y","peak_risk_Y")} for mp in maxposs},
            "cap5_5bp_unweighted": {k: u[k] for k in ("net_Y","pf","win_rate","executed","mdd_Y","peak_risk_Y")},
            "cap5_5bp_weighted": {"net_Y_mean": m(wY), "net_Y_median": float(np.median(wY)),
                                  "net_Y_min": min(wY), "net_Y_max": max(wY),
                                  "positive": sum(1 for x in wY if x>0), "beats_unw": sum(1 for x in wY if x>u['net_Y']),
                                  "pf_mean": m(wPF), "wr_mean": m(wWR), "mdd_Y_mean": m(wDD),
                                  "exec_mean": m([s['executed'] for s in w_stats])},
        }

    os.makedirs("/app/runtime/bt_out", exist_ok=True)
    fn = f"/app/runtime/bt_out/fusion_bt_multi{('_'+args.tag) if args.tag else ''}.json"
    with open(fn, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n已落盘: {fn}")


if __name__ == "__main__":
    main()
