# -*- coding: utf-8 -*-
"""Step 13: 提取 2026 年 MA/EG 1分钟数据 + 覆盖度检查"""
import zipfile, pandas as pd, numpy as np

SRC = {
    "MA": ("D:/学习资料/CZCE_郑州商品交易所.zip", "MA_甲醇/00_主力连续/MA_main_1m.csv"),
    "EG": ("D:/学习资料/DCE_大连商品交易所.zip", "EG_乙二醇/00_主力连续/EG_main_1m.csv"),
}
OUT = "E:/抖音分析/2026-10-16-19-50-44/kline_encoding"

for sym, (zip_path, member) in SRC.items():
    kept = 0
    out_path = f"{OUT}/{sym}_2026_1m_raw.csv"
    with zipfile.ZipFile(zip_path) as z, open(out_path, "w", newline="", encoding="utf-8") as fo:
        with z.open(member) as fi:
            header = fi.readline().decode("utf-8-sig").strip()
            fo.write(header + "\n")
            for raw in fi:
                line = raw.decode("utf-8", errors="replace")
                if line[:4] == "2026":
                    fo.write(line if line.endswith("\n") else line + "\n")
                    kept += 1
    print(f"{sym}: kept2026={kept:,}")

for sym in ["MA", "EG"]:
    d = pd.read_csv(f"{OUT}/{sym}_2026_1m_raw.csv", parse_dates=["datetime"])
    print(f"\n== {sym} 2026 == rows={len(d):,} range={d.datetime.min()} -> {d.datetime.max()}")
    print("  days:", d.datetime.dt.date.nunique())
    cnt = d.datetime.dt.date.value_counts()
    print("  bars/day min/median/max:", cnt.min(), int(cnt.median()), cnt.max())
    nonight = cnt[cnt == 225]
    print("  无夜盘日(225根):", len(nonight), [str(x) for x in nonight.index[:10]])
    print("  close NaN:", d.close.isna().sum(), "| 重复时间戳:", d.datetime.duplicated().sum())
    print("  价格 min/max:", d.close.min(), d.close.max())
    # 与 2020-2025 的会话结构一致性
    d21 = pd.read_csv(f"{OUT}/{sym}_2021_2025_1m_raw.csv", parse_dates=["datetime"])
    d21 = d21[d21.datetime.dt.year == 2025]
    cnt21 = d21.datetime.dt.date.value_counts()
    print("  2025 bars/day min/median/max:", cnt21.min(), int(cnt21.median()), cnt21.max())
    # 2026 月度分布
    m = d.datetime.dt.to_period("M").value_counts().sort_index()
    print("  月度根数:", {str(k): int(v) for k, v in m.items()})
