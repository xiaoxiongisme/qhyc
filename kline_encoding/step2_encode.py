# -*- coding: utf-8 -*-
"""Step 2: 按用户方案把1分钟K线编码为四元组 (6位二进制形态码, 起始位, 涨跌幅%, 振幅%)

编码规则（用户方案）：
  6位二进制 = [实体方向 2位][上影线 2位][下影线 2位]
    实体方向: "00"=跌(close<open), "01"=涨(close>=open, 含平盘)
    上影线  : "01"=有(high > max(open,close)), "00"=无
    下影线  : "01"=有(low  < min(open,close)), "00"=无
  四元组: (code, open, chg_pct=(close-open)/open*100, amp_pct=(high-low)/open*100)
"""
import pandas as pd
import numpy as np

OUT = "E:/抖音分析/2026-10-16-19-50-44/kline_encoding"

def encode(df):
    o, h, l, c = df.open.values, df.high.values, df.low.values, df.close.values
    body = np.where(c >= o, "01", "00")            # 涨(含平)->01, 跌->00
    upper = np.where(h > np.maximum(o, c), "01", "00")
    lower = np.where(l < np.minimum(o, c), "01", "00")
    code = np.char.add(np.char.add(body, upper), lower)
    chg = (c - o) / o * 100.0
    amp = (h - l) / o * 100.0
    return code, chg, amp

frames = []
for sym in ["MA", "EG"]:
    df = pd.read_csv(f"{OUT}/{sym}_2020_1m_raw.csv", parse_dates=["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    code, chg, amp = encode(df)
    enc = pd.DataFrame({
        "datetime": df.datetime.dt.strftime("%Y-%m-%d %H:%M:%S"),
        "code": code,
        "code_dec": [int(cd, 2) for cd in code],   # 十进制定律, 防止Excel/pandas按数字读取时丢失前导零
        "open": df.open,
        "chg_pct": np.round(chg, 4),
        "amp_pct": np.round(amp, 4),
    })
    enc.to_csv(f"{OUT}/{sym}_2020_1m_encoded.csv", index=False)
    # 全字段分析用
    full = df[["datetime", "open", "high", "low", "close", "volume"]].copy()
    full["code"] = code
    full["code_dec"] = enc["code_dec"].values
    full["chg_pct"] = chg
    full["amp_pct"] = amp
    full["sym"] = sym
    frames.append(full)
    print(f"{sym}: {len(enc):,} bars encoded -> {sym}_2020_1m_encoded.csv")
    print(enc.code.value_counts().sort_index().to_string())

allf = pd.concat(frames, ignore_index=True)
allf.to_pickle(f"{OUT}/all_2020_1m.pkl")
print("pickle saved:", allf.shape)
