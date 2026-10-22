# -*- coding: utf-8 -*-
"""Step 15: 任务③ 量价复合 —— 严格三级协议
发现期 2020-2024 扫描 -> 中间验证 2025 -> 一次性 holdout 2026
特征(全部 t 时刻可知): vol_ratio, vol_z, vol_spike, oi_chg, oi_trend
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
    d2 = pd.read_csv(f"{OUT}/{sym}_2026_1m_raw.csv", parse_dates=["datetime"])
    d2["year"] = 2026; frames.append(d2)
    return pd.concat(frames, ignore_index=True).sort_values("datetime").reset_index(drop=True)

def prep(sym):
    d = load_all(sym)
    o, h, l, c = d.open.values, d.high.values, d.low.values, d.close.values
    d["code"] = encode_ohlc(o, h, l, c)
    d["chg"] = (c - o) / o * 100.0
    d["amp"] = (h - l) / o * 100.0
    gap = d.datetime.diff().dt.total_seconds().values
    adj = np.zeros(len(d), bool); adj[1:] = gap[1:] == 60.0
    d["adj"] = adj
    d["year"] = d.datetime.dt.year.values
    d["day"] = d.datetime.dt.date.values
    # ---- 量价特征 (只用 t 及之前的信息) ----
    vol = d.volume.values.astype(float)
    vol_s = pd.Series(vol)
    m20 = vol_s.shift(1).rolling(20, min_periods=10).mean().values
    s20 = vol_s.shift(1).rolling(20, min_periods=10).std().values
    d["vol_ratio"] = vol / np.where(m20 > 0, m20, np.nan)
    d["vol_z"] = (vol - m20) / np.where(s20 > 0, s20, np.nan)
    d["vol_spike"] = (d.vol_ratio > 2.0).values          # 放量(相对前20分钟)
    d["vol_quiet"] = (d.vol_ratio < 0.5).values          # 缩量
    oi = d.close_interest.values.astype(float)
    d["oi_chg"] = np.concatenate([[np.nan], np.diff(oi)]) / np.where(oi > 0, oi, np.nan)
    d["oi_up"] = (d.oi_chg > 0).values                   # 持仓增
    d["oi_dn"] = (d.oi_chg < 0).values                   # 持仓减
    return d

def t_of(x):
    x = np.asarray(x); x = x[~np.isnan(x)]
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 and x.std(ddof=1) > 0 else 0.0

def stat(prev_mask, d, year_set=None):
    """prev_mask: 信号根(前一根)的条件; 返回下一根 chg 统计"""
    sel = d.adj.values[1:] & prev_mask[:-1]
    if year_set is not None:
        sel &= np.isin(d.year.values[:-1], list(year_set))
    x = d.chg.values[1:][sel]
    x = x[~np.isnan(x)]
    if len(x) < 30: return None
    return {"n": int(len(x)), "mean": float(x.mean()), "t": t_of(x), "win": float((x > 0).mean())}

RES = {"scan": {}, "selected": {}, "econ": {}, "vol_curve": {}}
for sym in ["MA", "EG"]:
    print(f"\n{'='*26} {sym} {'='*26}")
    d = prep(sym)
    mult = 10.0  # 吨/手
    price = {y: float(d.close.values[d.year.values == y].mean()) for y in [2020, 2021, 2022, 2023, 2024, 2025, 2026]}
    print("  各年均价:", {k: round(v) for k, v in price.items()})

    # ============ 扫描: 8形态 × 量比5档 (发现期 2020-2024) ============
    vr = d.vol_ratio.values
    qs = np.nanpercentile(vr[d.year.values <= 2024], [20, 40, 60, 80])
    bins = [("Q1最小", vr <= qs[0]), ("Q2", (vr > qs[0]) & (vr <= qs[1])), ("Q3", (vr > qs[1]) & (vr <= qs[2])),
            ("Q4", (vr > qs[2]) & (vr <= qs[3])), ("Q5最大", vr > qs[3])]
    scan = {}
    n_cells = 0
    for s in STATES:
        scan[s] = {}
        for bname, bm in bins:
            st = stat((d.code.values == s) & bm, d, range(2020, 2025))
            n_cells += 1
            scan[s][bname] = st
    RES["scan"][sym] = {s: {k: v for k, v in v0.items()} for s, v0 in scan.items()}
    print(f"  [扫描] {n_cells} 个 形态×量比 单元 (发现期2020-2024), 量比分位 q20-80 = {[round(q,2) for q in qs]}")
    print("    形态 -> 各量比档 下一根mean%(t):")
    for s in STATES:
        cells = []
        for bname, _ in bins:
            v = scan[s][bname]
            cells.append(f"{v['mean']:+.4f}({v['t']:+.0f})" if v else "n/a")
        print(f"      {s}: " + " | ".join(cells))

    # ============ 候选规则筛选: 发现期 |t|>5 且 n>500, 按 |t| 排序 ============
    cands = []
    # 规则族A: 形态 × 量比分档
    for s in STATES:
        for bname, _ in bins:
            v = scan[s][bname]
            if v and abs(v["t"]) > 5 and v["n"] > 500:
                cands.append({"rule": f"{s}×{bname}", "type": "vol_bin", "state": s, "bin": bname, **v})
    # 规则族B: 形态 × 放量/缩量二值
    for s in CORE:
        for fname, fmask in [("放量", d.vol_spike.values), ("缩量", d.vol_quiet.values)]:
            v = stat((d.code.values == s) & fmask, d, range(2020, 2025))
            if v and abs(v["t"]) > 5 and v["n"] > 500:
                cands.append({"rule": f"{s}×{fname}", "type": "vol_binary", "state": s, "bin": fname, **v})
    # 规则族C: 形态 × OI增/减
    for s in CORE:
        for fname, fmask in [("OI增", d.oi_up.values), ("OI减", d.oi_dn.values)]:
            v = stat((d.code.values == s) & fmask, d, range(2020, 2025))
            if v and abs(v["t"]) > 5 and v["n"] > 500:
                cands.append({"rule": f"{s}×{fname}", "type": "oi_binary", "state": s, "bin": fname, **v})
    # 规则族D: 全形态×放量OI增 等三元
    for s in CORE:
        v = stat((d.code.values == s) & d.vol_spike.values & d.oi_up.values, d, range(2020, 2025))
        if v and abs(v["t"]) > 5 and v["n"] > 500:
            cands.append({"rule": f"{s}×放量×OI增", "type": "triple", "state": s, "bin": "放量OI增", **v})
    cands.sort(key=lambda x: -abs(x["t"]))
    print(f"  [筛选] 扫描候选 {len(cands)} 条 (发现期|t|>5 & n>500), top10:")
    for c in cands[:10]:
        print(f"    {c['rule']:24s} n={c['n']:>6} mean={c['mean']:+.4f}% t={c['t']:+.1f} win={c['win']:.3f}")

    # ============ 锁定规则, 2025 中间验证 + 2026 holdout ============
    # 选择标准: 发现期 top 且与已知规律方向一致, 每类取最强1-2条
    sel_rules = []
    seen_types = {}
    for c in cands:
        key = c["type"]
        if seen_types.get(key, 0) >= 2: continue
        seen_types[key] = seen_types.get(key, 0) + 1
        sel_rules.append(c)
    # 同时保留"纯形态"基线(无复合)作对照
    base_rules = []
    for s in CORE:
        v = stat(d.code.values == s, d, range(2020, 2025))
        base_rules.append({"rule": f"{s}(纯形态基线)", "type": "base", "state": s, **v})
    RES["selected"][sym] = {"rules": sel_rules, "base": base_rules}
    print("  [锁定规则] 每族取发现期最强 ≤2 条:")
    locked = []
    for c in sel_rules + base_rules:
        # 重建 mask
        if c["type"] == "base":
            m = (d.code.values == c["state"])
        elif c["type"] == "vol_bin":
            bname = c["bin"]; bm = dict(bins)[bname]; m = (d.code.values == c["state"]) & bm
        elif c["type"] == "vol_binary":
            m = (d.code.values == c["state"]) & (d.vol_spike.values if c["bin"] == "放量" else d.vol_quiet.values)
        elif c["type"] == "oi_binary":
            m = (d.code.values == c["state"]) & (d.oi_up.values if c["bin"] == "OI增" else d.oi_dn.values)
        else:
            m = (d.code.values == c["state"]) & d.vol_spike.values & d.oi_up.values
        v25 = stat(m, d, [2025]); v26 = stat(m, d, [2026])
        p25 = price[2025]; p26 = price[2026]
        e25 = v25["mean"] / 100 * p25 * mult if v25 else None
        e26 = v26["mean"] / 100 * p26 * mult if v26 else None
        locked.append({**c, "v2025": v25, "v2026": v26, "edge25": e25, "edge26": e26})
        s25 = f"25:{v25['mean']:+.4f}%(t{v25['t']:+.1f},n{v25['n']})" if v25 else "25:n/a"
        s26 = f"26:{v26['mean']:+.4f}%(t{v26['t']:+.1f},n{v26['n']})" if v26 else "26:n/a"
        print(f"    {c['rule']:24s} 发现:{c['mean']:+.4f}(t{c['t']:+.1f}) | {s25} | {s26}"
              + (f" | 26收益:{e26:+.1f}元/手" if e26 is not None else ""))
    RES["econ"][sym] = {"locked": locked, "price": price, "cost_rt": 20.0, "mult": mult}

json.dump(RES, open(f"{OUT}/results_qv.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("\nsaved results_qv.json")
