# -*- coding: utf-8 -*-
"""Step 7: 汇总 2020-2025 全8形态统计 + 生成 V2 报告(跨周期/跨年复验 + 八卦视角)"""
import pandas as pd, numpy as np, json
from scipy import stats

OUT = "E:/抖音分析/2026-10-16-19-50-44/kline_encoding"
STATES = ["000000", "000001", "000100", "000101", "010000", "010001", "010100", "010101"]
YRS = json.load(open(f"{OUT}/results_years.json", encoding="utf-8"))
HTF = json.load(open(f"{OUT}/results_htf.json", encoding="utf-8"))

def encode_ohlc(o, h, l, c):
    body = np.where(c >= o, "01", "00")
    upper = np.where(h > np.maximum(o, c), "01", "00")
    lower = np.where(l < np.minimum(o, c), "01", "00")
    return np.char.add(np.char.add(body, upper), lower)

def sess_key(dt):
    h = dt.hour + dt.minute / 60
    s = 0 if 9 <= h < 11.5 else (1 if 13.5 <= h < 15 else (2 if 21 <= h < 23 else -1))
    return dt.normalize(), s

# ---- 汇总 2020-2025 全8形态的下一根统计 ----
POOL = {}
for sym in ["MA", "EG"]:
    d = pd.read_csv(f"{OUT}/{sym}_2021_2025_1m_raw.csv", parse_dates=["datetime"])
    d0 = pd.read_csv(f"{OUT}/{sym}_2020_1m_raw.csv", parse_dates=["datetime"])
    d = pd.concat([d, d0], ignore_index=True).sort_values("datetime").reset_index(drop=True)
    d["dt"] = d.datetime
    o, h, l, c = d.open.values, d.high.values, d.low.values, d.close.values
    code = encode_ohlc(o, h, l, c)
    chg = (c - o) / o * 100.0
    up = c > o; dj = c == o
    keys = [sess_key(t) for t in d.datetime]
    tkey = np.array([k[0].value * 10 + k[1] for k in keys])
    cont = tkey[1:] == tkey[:-1]
    s_now, s_nxt = code[:-1][cont], code[1:][cont]
    chg_n, up_n = chg[1:][cont], up[1:][cont]
    price = float(c.mean())
    st = {}
    for s in STATES:
        m = s_now == s
        x = chg_n[m]
        t, _ = stats.ttest_1samp(x, 0)
        st[s] = {"n": int(m.sum()), "p_up": float(up_n[m].mean()), "mean": float(x.mean()), "t": float(t)}
    POOL[sym] = {"n": int(len(d)), "price": price, "states": st,
                 "acf1_abs": float(pd.Series(np.abs(chg)).autocorr(1)),
                 "acf1_chg": float(pd.Series(chg).autocorr(1))}
    print(sym, "pooled n=", len(d), "price=", round(price))
    for s in STATES:
        x = st[s]
        print(f"   {s}: n={x['n']:,} mean={x['mean']:+.4f}% t={x['t']:+.1f} p_up={x['p_up']:.3f}")

json.dump(POOL, open(f"{OUT}/results_pool.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("saved results_pool.json")
