# -*- coding: utf-8 -*-
"""Step 10: 控制变量 + 多步持有 + 5m/15m口径修正 + bootstrap
V6 控制变量: 前根振幅分层 / 时段分层 / CLV收盘位置机制检验 / 影线长度敏感性
V7 多步持有: 信号后 1/2/3/5/10 根累计收益(同会话)
V8 5m/15m口径: 桶内时间跨度检查 + 休市相邻对剔除后重算
V9 bootstrap(按日块) + Wilcoxon
"""
import pandas as pd, numpy as np, json
from scipy import stats

OUT = "E:/抖音分析/2026-10-16-19-50-44/kline_encoding"
STATES = ["000000", "000001", "000100", "000101", "010000", "010001", "010100", "010101"]
CORE = ["010000", "000000", "000100", "010001"]

def encode_ohlc(o, h, l, c):
    body = np.where(c >= o, "01", "00")
    upper = np.where(h > np.maximum(o, c), "01", "00")
    lower = np.where(l < np.minimum(o, c), "01", "00")
    return np.char.add(np.char.add(body, upper), lower)

def sess_key(dt):
    h = dt.hour + dt.minute / 60
    s = 0 if 9 <= h < 11.5 else (1 if 13.5 <= h < 15 else (2 if 21 <= h < 23 else -1))
    return dt.normalize(), s

def load_all(sym):
    frames = []
    d0 = pd.read_csv(f"{OUT}/{sym}_2020_1m_raw.csv", parse_dates=["datetime"])
    d0["year"] = 2020; frames.append(d0)
    d1 = pd.read_csv(f"{OUT}/{sym}_2021_2025_1m_raw.csv", parse_dates=["datetime"])
    d1["year"] = d1.datetime.dt.year; frames.append(d1)
    return pd.concat(frames, ignore_index=True).sort_values("datetime").reset_index(drop=True)

def t_of(x):
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 and x.std(ddof=1) > 0 else 0.0

RES = {"control": {}, "multihold": {}, "htf_fix": {}, "bootstrap": {}}

for sym in ["MA", "EG"]:
    print(f"\n{'='*28} {sym} {'='*28}")
    d = load_all(sym)
    o, h, l, c = d.open.values, d.high.values, d.low.values, d.close.values
    d["dt"] = d.datetime
    d["code"] = encode_ohlc(o, h, l, c)
    chg = (c - o) / o * 100.0
    d["chg"] = chg
    d["amp"] = (h - l) / o * 100.0
    keys = [sess_key(t) for t in d.datetime]
    tkey = np.array([k[0].value * 10 + k[1] for k in keys])
    gap = d.datetime.diff().dt.total_seconds().values
    adj = np.zeros(len(d), bool); adj[1:] = gap[1:] == 60.0     # 严格相邻
    d["adj"] = adj
    d["day"] = d.datetime.dt.date.values
    d["hour"] = d.datetime.dt.hour.values
    # CLV 收盘位置
    rng_ = np.where(h > l, h - l, np.nan)
    d["clv"] = np.where(h > l, (c - l) / np.where(h > l, h - l, 1), 0.5)
    # 影线长度(相对开盘价%)
    d["up_len"] = np.where(h > np.maximum(o, c), (h - np.maximum(o, c)) / o * 100, 0.0)
    d["lo_len"] = np.where(l < np.minimum(o, c), (np.minimum(o, c) - l) / o * 100, 0.0)
    n = len(d)
    # 信号样本索引: (t 是信号, t+1 合法相邻)
    sig_idx = {}
    for s in CORE:
        prev = (d.code.values[:-1] == s)
        sel = adj[1:] & prev
        sig_idx[s] = np.where(sel)[0] + 1   # t+1 的行号
    chg_v = d.chg.values
    day_v = d.day

    # ---- V6a 前根振幅分层 ----
    ctrl = {}
    for s in CORE:
        ti = sig_idx[s]; ti_prev = ti - 1
        amp_p = d.amp.values[ti_prev]
        q1, q2 = np.percentile(amp_p, [33.3, 66.7])
        rows = {}
        for name, m in [("小振幅", amp_p <= q1), ("中振幅", (amp_p > q1) & (amp_p <= q2)), ("大振幅", amp_p > q2)]:
            x = chg_v[ti[m]]
            rows[name] = {"n": int(len(x)), "mean": float(x.mean()) if len(x) else None,
                          "t": t_of(x) if len(x) > 2 else None}
        ctrl[s] = {"by_amp": rows, "amp_q": [float(q1), float(q2)]}
    # ---- V6b 时段分层 ----
    seg_def = [("夜盘21-23", lambda hh: (hh >= 21)), ("早盘09-10:15", lambda hh: (hh == 9) | ((hh == 10) & True)),
               ("上午10:30-11:30", lambda hh: (hh == 10) | (hh == 11)),
               ("午盘13:30-15:00", lambda hh: hh == 13)]
    def seg_of(hh, mm):
        if hh >= 21: return "夜盘21-23"
        if hh == 9: return "早盘09-10:15"
        if hh == 10: return "早盘09-10:15" if mm < 15 else "上午10:30-11:30"
        if hh == 11: return "上午10:30-11:30"
        if hh == 13: return "午盘13:30-15:00"
        if hh == 14: return "午盘13:30-15:00"
        return "其他"
    hh = d.datetime.dt.hour.values; mm = d.datetime.dt.minute.values
    seg_v = np.array([seg_of(a, b) for a, b in zip(hh, mm)])
    d["seg"] = seg_v
    for s in CORE:
        rows = {}
        for seg in ["夜盘21-23", "早盘09-10:15", "上午10:30-11:30", "午盘13:30-15:00"]:
            m = (seg_v[sig_idx[s]] == seg)
            x = chg_v[sig_idx[s][m]]
            rows[seg] = {"n": int(len(x)), "mean": float(x.mean()) if len(x) else None,
                         "t": t_of(x) if len(x) > 2 else None}
        ctrl[s]["by_seg"] = rows
    # ---- V6c CLV 机制检验: 所有K线按 CLV 分10档, 看次日效应 ----
    clv_v = d.clv.values
    clv_rows = []
    for lo, hi in zip(np.arange(0, 1, 0.1), np.arange(0.1, 1.01, 0.1)):
        m_prev = (clv_v[:-1] >= lo - 1e-9) & (clv_v[:-1] < hi + 1e-9) & adj[1:]
        x = chg_v[1:][m_prev]
        if len(x) > 30:
            clv_rows.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": int(len(x)), "mean": float(x.mean()), "t": t_of(x)})
    RES["control"][sym] = ctrl
    RES["control"][sym]["_clv"] = clv_rows
    # ---- V6d 影线长度敏感性: 010000 vs 010001(下影长度分层) ----
    sens = {}
    for s in ["010001", "000100"]:
        ti = sig_idx[s]; ti_prev = ti - 1
        lv = d.up_len.values[ti_prev] if s == "000100" else d.lo_len.values[ti_prev]
        rows = []
        for lo, hi in [(0, 0.02), (0.02, 0.06), (0.06, 0.15), (0.15, 1e9)]:
            m = (lv >= lo) & (lv < hi)
            x = chg_v[ti[m]]
            if len(x) > 30:
                rows.append({"bin": f"{lo:.2f}-{hi:.2f}%" if hi < 1e8 else f">{lo:.2f}%", "n": int(len(x)),
                             "mean": float(x.mean()), "t": t_of(x)})
        sens[s] = rows
    RES["control"][sym]["_shadow_len"] = sens
    print("  [振幅分层] (前根振幅小/中/大 -> mean% t)")
    for s in CORE:
        r = ctrl[s]["by_amp"]
        print(f"    {s}: " + " | ".join(f"{k}: {v['mean']:+.4f}({v['t']:+.1f})n={v['n']}" for k, v in r.items() if v["mean"] is not None))
    print("  [时段分层]")
    for s in CORE:
        r = ctrl[s]["by_seg"]
        print(f"    {s}: " + " | ".join(f"{k[:4]}: {v['mean']:+.4f}({v['t']:+.1f})n={v['n']}" for k, v in r.items() if v["mean"] is not None))
    print("  [CLV机制] 阳线按收盘位置10档 -> 次日mean%:")
    print("    " + " ".join(f"{r['bin']}:{r['mean']:+.4f}(t{r['t']:+.0f},n{r['n']})" for r in clv_rows))

    # ---- V7 多步持有 ----
    # 累计收益 close_{t+k}/close_t - 1, 同会话内连续 k 根
    mh = {}
    c_v = d.close.values
    for s in CORE:
        ti = sig_idx[s]
        rows = []
        for k in [1, 2, 3, 5, 10]:
            ti_k = ti[ti + k - 1 < n]
            # t+k 必须与 t+1 同会话连续 (gap链: t+1..t+k 每步 gap==60)
            ok = np.ones(len(ti_k), bool)
            for j in range(1, k):
                nxt = ti_k + j
                ok &= (gap[nxt] == 60.0)
            idx = ti_k[ok]
            if len(idx) < 30: continue
            x = (c_v[idx + k - 1] - c_v[idx - 1]) / c_v[idx - 1] * 100.0
            rows.append({"k": k, "n": int(len(x)), "mean": float(x.mean()), "t": t_of(x)})
        mh[s] = rows
    RES["multihold"][sym] = mh
    print("  [多步持有] 信号后持有k根累计收益%:")
    for s in CORE:
        print(f"    {s}: " + " | ".join(f"k{r['k']}: {r['mean']:+.4f}(t{r['t']:+.1f},n{r['n']})" for r in mh[s]))

    # ---- V9 bootstrap 按日块 ----
    rng = np.random.default_rng(42)
    bs = {}
    for s in CORE:
        ti = sig_idx[s]
        x = chg_v[ti]; days = day_v[ti]
        ud = np.unique(days)
        day_mean = np.array([x[days == u].mean() for u in ud])
        B = 200
        draws = rng.choice(day_mean, size=(B, len(day_mean)), replace=True).mean(1)
        lo, hi = np.percentile(draws, [2.5, 97.5])
        w_stat, w_p = stats.wilcoxon(day_mean)
        bs[s] = {"daily_mean": float(day_mean.mean()), "ci_lo": float(lo), "ci_hi": float(hi),
                 "wilcoxon_p": float(w_p), "n_days": int(len(day_mean))}
    RES["bootstrap"][sym] = bs
    print("  [bootstrap按日块] 95%CI 与 Wilcoxon:")
    for s in CORE:
        v = bs[s]
        print(f"    {s}: daily_mean={v['daily_mean']:+.4f}% CI[{v['ci_lo']:+.4f},{v['ci_hi']:+.4f}] wilcoxon_p={v['wilcoxon_p']:.2e}")

json.dump(RES, open(f"{OUT}/results_verify2.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("\nsaved results_verify2.json (part 1)")
