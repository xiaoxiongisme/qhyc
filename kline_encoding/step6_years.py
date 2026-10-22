# -*- coding: utf-8 -*-
"""Step 6: 提取 2021-2025 年 MA/EG 1分钟数据, 同规则编码, 分年+合并复算核心统计量"""
import zipfile, pandas as pd, numpy as np, json
from scipy import stats

OUT = "E:/抖音分析/2026-10-16-19-50-44/kline_encoding"
SRC = {
    "MA": ("D:/学习资料/CZCE_郑州商品交易所.zip", "MA_甲醇/00_主力连续/MA_main_1m.csv"),
    "EG": ("D:/学习资料/DCE_大连商品交易所.zip", "EG_乙二醇/00_主力连续/EG_main_1m.csv"),
}
STATES = ["000000", "000001", "000100", "000101", "010000", "010001", "010100", "010101"]
YEARS = [2020, 2021, 2022, 2023, 2024, 2025]

def encode_ohlc(o, h, l, c):
    body = np.where(c >= o, "01", "00")
    upper = np.where(h > np.maximum(o, c), "01", "00")
    lower = np.where(l < np.minimum(o, c), "01", "00")
    return np.char.add(np.char.add(body, upper), lower)

def sess_key(dt):
    h = dt.hour + dt.minute / 60
    if 9 <= h < 11.5: s = 0
    elif 13.5 <= h < 15: s = 1
    elif 21 <= h < 23: s = 2
    else: s = -1
    return dt.normalize(), s

def core_stats(d, sym, tag):
    o, h, l, c = d.open.values, d.high.values, d.low.values, d.close.values
    code = encode_ohlc(o, h, l, c)
    chg = (c - o) / o * 100.0; amp = (h - l) / o * 100.0
    n = len(d)
    up = c > o; dj = c == o
    keys = [sess_key(t) for t in d.dt]
    tkey = np.array([k[0].value * 10 + k[1] for k in keys])
    cont = tkey[1:] == tkey[:-1]
    s_now, s_nxt = code[:-1][cont], code[1:][cont]
    up_n = up[1:][cont]; chg_n = chg[1:][cont]
    r = {"sym": sym, "tag": tag, "n": int(n)}
    r["doji_rate"] = float(dj.mean()); r["up_exdoji"] = float(up[~dj].mean()); r["amp_med"] = float(np.median(amp))
    idx = {s: i for i, s in enumerate(STATES)}
    cmh = np.zeros((8, 8))
    np.add.at(cmh, (np.array([idx[s] for s in s_now]), np.array([idx[s] for s in s_nxt])), 1)
    pxy = cmh / cmh.sum(); nz = pxy > 0
    r["mi"] = float((pxy[nz] * np.log2(pxy[nz] / (pxy.sum(1, keepdims=True) @ pxy.sum(0, keepdims=True))[nz])).sum())
    for s in ["010000", "000000", "010001", "000100", "010101"]:
        m = s_now == s
        if m.sum() > 20:
            x = chg_n[m]
            t, _ = stats.ttest_1samp(x, 0)
            r[f"st_{s}"] = {"n": int(m.sum()), "p_up": float(up_n[m].mean()), "mean": float(x.mean()), "t": float(t)}
    r["acf1_abs"] = float(pd.Series(np.abs(chg)).autocorr(1))
    r["acf1_chg"] = float(pd.Series(chg).autocorr(1))
    price = float(c.mean())
    r["price"] = price
    if "st_010000" in r: r["edge_short"] = r["st_010000"]["mean"] / 100 * price * 10
    if "st_000100" in r: r["edge_long"] = r["st_000100"]["mean"] / 100 * price * 10
    return r

# 提取 2021-2025 (2020 用已有文件)
frames = {"MA": [], "EG": []}
for sym, (zp, member) in SRC.items():
    with zipfile.ZipFile(zp) as z:
        rows = []
        with z.open(member) as fi:
            header = fi.readline().decode("utf-8-sig")
            for raw in fi:
                line = raw.decode("utf-8", errors="replace")
                y = line[:4]
                if y in ("2021", "2022", "2023", "2024", "2025"):
                    rows.append(line)
        with open(f"{OUT}/{sym}_2021_2025_1m_raw.csv", "w", encoding="utf-8") as fo:
            fo.write(header)
            fo.writelines(rows)
    d = pd.read_csv(f"{OUT}/{sym}_2021_2025_1m_raw.csv", parse_dates=["datetime"])
    d["dt"] = d.datetime
    d["year"] = d.dt.dt.year
    frames[sym].append(d)
    print(sym, "2021-2025 rows:", len(d))

# 2020
for sym in ["MA", "EG"]:
    d0 = pd.read_csv(f"{OUT}/{sym}_2020_1m_raw.csv", parse_dates=["datetime"])
    d0["dt"] = d0.datetime; d0["year"] = 2020
    frames[sym].append(d0)

RES = []
for sym in ["MA", "EG"]:
    d = pd.concat(frames[sym], ignore_index=True).sort_values("dt").reset_index(drop=True)
    d = d[d.dt.dt.year.isin(YEARS)]
    for y in YEARS + ["ALL"]:
        dd = d if y == "ALL" else d[d.year == y]
        if len(dd) < 1000: continue
        r = core_stats(dd, sym, str(y))
        RES.append(r)
        st = r.get("st_010000"); st2 = r.get("st_000100"); st3 = r.get("st_010001")
        print(f"{sym} {y}: n={r['n']:,} doji={r['doji_rate']:.1%} MI={r['mi']:.4f} acf1|chg|={r['acf1_abs']:.3f} "
              f"| 010000: n={st['n'] if st else 0} mean={st['mean']:+.4f}% t={st['t']:+.1f} "
              f"| 000100: n={st2['n'] if st2 else 0} mean={st2['mean']:+.4f}% t={st2['t']:+.1f} "
              f"| 010001: n={st3['n'] if st3 else 0} mean={st3['mean']:+.4f}% t={st3['t']:+.1f}")

json.dump(RES, open(f"{OUT}/results_years.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("saved results_years.json")
