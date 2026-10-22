# -*- coding: utf-8 -*-
"""Step 5: 会话感知重采样 1m -> 5m/15m, 同规则编码 + 核心统计量复算"""
import pandas as pd, numpy as np, json
from scipy import stats

OUT = "E:/抖音分析/2026-10-16-19-50-44/kline_encoding"
STATES = ["000000", "000001", "000100", "000101", "010000", "010001", "010100", "010101"]

def encode_ohlc(o, h, l, c):
    body = np.where(c >= o, "01", "00")
    upper = np.where(h > np.maximum(o, c), "01", "00")
    lower = np.where(l < np.minimum(o, c), "01", "00")
    return np.char.add(np.char.add(body, upper), lower)

def session_key(dt):
    d = dt.normalize()
    h = dt.hour + dt.minute / 60
    if 9 <= h < 11.5: s = 0
    elif 13.5 <= h < 15: s = 1
    elif 21 <= h < 23: s = 2
    else: s = -1
    return d, s

def resample(df, mins):
    """会话内聚合: 返回 5m/15m OHLCV"""
    df = df.sort_values("dt").reset_index(drop=True)
    keys = [session_key(t) for t in df.dt]
    df["sdate"] = [k[0] for k in keys]; df["sess"] = [k[1] for k in keys]
    # 会话内序号
    df = df[df.sess >= 0].copy()
    df["seq"] = df.groupby(["sdate", "sess"]).cumcount()
    df["bucket"] = df["seq"] // mins
    g = df.groupby(["sdate", "sess", "bucket"])
    agg = g.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                close=("close", "last"), volume=("volume", "sum"), nbar=("close", "size"))
    agg = agg[agg.nbar == mins].reset_index()   # 丢弃不满一根的残桶(本数据恰无)
    return agg

def core_stats(d, sym, tf):
    o, h, l, c = d.open.values, d.high.values, d.low.values, d.close.values
    code = encode_ohlc(o, h, l, c)
    chg = (c - o) / o * 100.0; amp = (h - l) / o * 100.0
    n = len(d)
    up = c > o; dn = c < o; dj = c == o
    s_now, s_nxt = code[:-1], code[1:]
    # 同会话内相邻(按时间序，已按 sdate,sess 分桶; 跨会话/隔夜的相邻对用时间戳过滤)
    tkey = d.sdate.astype("int64") * 10 + d.sess
    cont = tkey[1:].values == tkey[:-1].values
    s_now, s_nxt = s_now[cont], s_nxt[cont]
    r = {"sym": sym, "tf": tf, "n": int(n)}
    r["doji_rate"] = float(dj.mean()); r["up_rate"] = float((c >= o).mean()); r["up_rate_exdoji"] = float(up[~dj].mean())
    r["amp_med"] = float(np.median(amp))
    vc = pd.Series(code).value_counts()
    r["state_pct"] = {s: float(vc.get(s, 0) / n * 100) for s in STATES}
    up_n = up[1:][cont]; dn_n = dn[1:][cont]; dj_n = dj[1:][cont]
    chg_n = chg[1:][cont]
    for s in STATES:
        m = s_now == s
        if m.sum() > 30:
            x = chg_n[m]
            t, p = stats.ttest_1samp(x, 0)
            r[f"st_{s}"] = {"n": int(m.sum()), "p_up": float(up_n[m].mean()), "p_dn": float(dn_n[m].mean()),
                            "p_doji": float(dj_n[m].mean()), "mean": float(x.mean()), "t": float(t)}
    # MI / 自相关 / 同形态延续
    idx = {s: i for i, s in enumerate(STATES)}
    cmh = np.zeros((8, 8))
    xi = np.array([idx[s] for s in s_now]); yi = np.array([idx[s] for s in s_nxt])
    np.add.at(cmh, (xi, yi), 1)
    pxy = cmh / cmh.sum(); px = pxy.sum(1, keepdims=True); py = pxy.sum(0, keepdims=True)
    nz = pxy > 0
    r["mi"] = float((pxy[nz] * np.log2(pxy[nz] / (px @ py)[nz])).sum())
    r["same_rate"] = float(np.trace(cmh) / cmh.sum())
    r["acf1_chg"] = float(pd.Series(chg).autocorr(1)); r["acf1_abs"] = float(pd.Series(np.abs(chg)).autocorr(1))
    r["acf5_abs"] = float(pd.Series(np.abs(chg)).autocorr(5)) if n > 2000 else None
    # 经济性: 010000后做空, 000100后做多 (元/手, 乘数10吨/手)
    price = float(c.mean())
    mult = 10.0
    if "st_010000" in r: r["edge_short_010000"] = r["st_010000"]["mean"] / 100 * price * mult
    if "st_000100" in r: r["edge_long_000100"] = r["st_000100"]["mean"] / 100 * price * mult
    r["price_mean"] = price
    return r

df = pd.read_pickle(f"{OUT}/all_2020_1m.pkl"); df["dt"] = pd.to_datetime(df.datetime)
RES = {"htf": []}
for sym in ["MA", "EG"]:
    d0 = df[df.sym == sym].sort_values("dt").reset_index(drop=True)
    for tf, mins in (("5m", 5), ("15m", 15)):
        agg = resample(d0.copy(), mins)
        agg.to_csv(f"{OUT}/{sym}_2020_{tf}_encoded_raw.csv", index=False)
        r = core_stats(agg, sym, tf)
        RES["htf"].append(r)
        print(f"{sym} {tf}: n={r['n']:,} doji={r['doji_rate']:.1%} up_exdoji={r['up_rate_exdoji']:.3f} "
              f"MI={r['mi']:.4f} acf1chg={r['acf1_chg']:.3f} acf1|chg|={r['acf1_abs']:.3f}")
        for s in ["010000", "000000", "010001", "000100"]:
            x = r.get(f"st_{s}")
            if x: print(f"   {s}: n={x['n']:,} 涨{x['p_up']:.1%}/跌{x['p_dn']:.1%} mean={x['mean']:+.4f}% t={x['t']:+.1f}")
        print(f"   edge: 010000做空={r.get('edge_short_010000', 0):+.1f}元/手, 000100做多={r.get('edge_long_000100', 0):+.1f}元/手, amp中位={r['amp_med']:.3f}%")

json.dump(RES, open(f"{OUT}/results_htf.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("saved results_htf.json")
