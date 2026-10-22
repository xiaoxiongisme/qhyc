# -*- coding: utf-8 -*-
"""Step 3b: 补充分析 —— 严格方向概率(剔除平盘膨胀)、形态几何结构、序列模式的条件收益、压缩率"""
import pandas as pd, numpy as np, json
from scipy import stats

OUT = "E:/抖音分析/2026-10-16-19-50-44/kline_encoding"
df = pd.read_pickle(f"{OUT}/all_2020_1m.pkl")
df["dt"] = pd.to_datetime(df.datetime)
df = df.sort_values(["sym", "dt"]).reset_index(drop=True)
STATES = ["000000", "000001", "000100", "000101", "010000", "010001", "010100", "010101"]

def two_prop_z(p1, n1, p2, n2):
    if n1 == 0 or n2 == 0: return 0.0, 1.0
    pp = (p1 * n1 + p2 * n2) / (n1 + n2)
    se = np.sqrt(max(pp * (1 - pp) * (1 / n1 + 1 / n2), 1e-18))
    z = (p1 - p2) / se
    return z, 2 * (1 - stats.norm.cdf(abs(z)))

R = json.load(open(f"{OUT}/analysis_results.json", encoding="utf-8"))
EXTRA = {}

for sym in ["MA", "EG"]:
    d = df[df.sym == sym].reset_index(drop=True)
    o, h, l, c = d.open.values, d.high.values, d.low.values, d.close.values
    code = np.asarray(d.code, dtype=object); chg = d.chg_pct.values; amp = d.amp_pct.values
    n = len(d)
    up = c > o          # 严格涨
    dn = c < o          # 严格跌
    dj = c == o         # 平盘
    rng = h - l
    body_abs = np.abs(c - o)
    up_sh = h - np.maximum(o, c)
    lo_sh = np.minimum(o, c) - l
    e = {}

    # 1) 每形态: 下一根 严格涨/严格跌/平盘 概率 + 条件均值
    s_now, s_nxt = code[:-1], code[1:]
    rows = []
    for s in STATES:
        m = s_now == s
        uu, dd, jj = up[1:][m], dn[1:][m], dj[1:][m]
        x = chg[1:][m]
        t, p = stats.ttest_1samp(x, 0)
        rows.append({"state": s, "n": int(m.sum()),
                     "p_up_strict": float(uu.mean()), "p_dn_strict": float(dd.mean()), "p_doji": float(jj.mean()),
                     "mean": float(x.mean()), "med": float(np.median(x)), "t": float(t)})
    e["state_strict"] = rows

    # 2) 形态几何: 平均振幅/实体占比/上影占比/下影占比
    geo = []
    for s in STATES:
        m = code == s
        rng_m = rng[m]; bz = body_abs[m] / np.maximum(rng_m, 1e-9)
        uz = up_sh[m] / np.maximum(rng_m, 1e-9); lz = lo_sh[m] / np.maximum(rng_m, 1e-9)
        geo.append({"state": s, "amp_mean": float(amp[m].mean()), "body_ratio": float(bz.mean()),
                    "up_ratio": float(uz.mean()), "lo_ratio": float(lz.mean()), "chg_abs_mean": float(np.abs(chg[m]).mean())})
    e["geometry"] = geo

    # 3) 连续同向K线根数 k -> 下一根严格涨概率 (剔除平盘样本内的连胜定义也用严格方向)
    streak_rows = []
    for k in range(0, 6):
        if k == 0:
            m = np.zeros(n, bool)  # 无条件
            base_m = np.ones(n, bool)
        else:
            m = np.ones(n, bool)
            for i in range(1, k + 1):
                m &= np.roll(up, i)
            m[:k] = False
        idxs = np.where(m)[0]
        idxs = idxs[idxs < n - 1]
        if len(idxs) > 50:
            streak_rows.append({"k": k, "n": int(len(idxs)), "p_up_strict": float(up[idxs + 1].mean()),
                                "p_dn_strict": float(dn[idxs + 1].mean()), "p_doji": float(dj[idxs + 1].mean()),
                                "mean_chg": float(chg[idxs + 1].mean())})
    e["streak"] = streak_rows

    # 4) 两形态序列 -> 下一根 条件收益 (含平盘剔除版win)
    pair = {}
    for i in range(n - 2):
        key = code[i] + "|" + code[i + 1]
        v = pair.setdefault(key, [0, 0, 0.0])
        v[0] += 1; v[1] += int(up[i + 2]); v[2] += chg[i + 2]
    pats = []
    for k_, (cnt, ups, sch) in pair.items():
        if cnt >= 400:
            pats.append({"pair": k_, "n": cnt, "p_up_strict": ups / cnt, "mean_next": sch / cnt})
    pats.sort(key=lambda x: -x["mean_next"])
    e["pattern_best"] = pats[:8]
    pats.sort(key=lambda x: x["mean_next"])
    e["pattern_worst"] = pats[:8]

    # 5) 信息熵 / 压缩
    p = np.array([ (code == s).mean() for s in STATES ])
    H = float(-(p[p > 0] * np.log2(p[p > 0])).sum())
    e["entropy_bits"] = H
    # 原始每根: datetime(19B)+OHLC+vol ≈ 60B; 编码每根 ≈ code(6B)+open+chg+amp ≈ 30B
    e["raw_bytes_per_bar"] = 19 + 4 * 7 + 8
    e["enc_bytes_per_bar"] = 19 + 6 + 4 * 8

    EXTRA[sym] = e

R["extra"] = EXTRA
with open(f"{OUT}/analysis_results.json", "w", encoding="utf-8") as f:
    json.dump(R, f, ensure_ascii=False, indent=1)

for sym in ["MA", "EG"]:
    e = EXTRA[sym]
    print("="*16, sym, "="*16)
    print("每形态 -> 下一根 [严格涨/严格跌/平盘] 与条件均值:")
    for x in e["state_strict"]:
        print("  %s n=%6d 涨%.3f 跌%.3f 平%.3f | mean=%+.4f%% t=%+.1f" % (x["state"], x["n"], x["p_up_strict"], x["p_dn_strict"], x["p_doji"], x["mean"], x["t"]))
    print("连胜k -> 下一根:", [(x["k"], x["n"], round(x["p_up_strict"],3), round(x["mean_chg"],4)) for x in e["streak"]])
    print("形态几何(amp均值, 实体占比, 上影占比, 下影占比):")
    for g in e["geometry"]:
        print("  %s amp=%.3f%% body=%.2f up=%.2f lo=%.2f" % (g["state"], g["amp_mean"], g["body_ratio"], g["up_ratio"], g["lo_ratio"]))
    print("最优两形态序列(按下一根均值):", [(p["pair"], p["n"], round(p["p_up_strict"],3), round(p["mean_next"],4)) for p in e["pattern_best"][:5]])
    print("最差:", [(p["pair"], p["n"], round(p["p_up_strict"],3), round(p["mean_next"],4)) for p in e["pattern_worst"][:5]])
    print("H(S)=%.3f bits" % e["entropy_bits"])
