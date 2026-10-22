# -*- coding: utf-8 -*-
"""Step 1: 从交易所zip中流式提取 甲醇(MA,CZCE) 与 乙二醇(EG,DCE) 2020年1分钟数据"""
import zipfile, io, csv, sys

SRC = {
    "MA": ("D:/学习资料/CZCE_郑州商品交易所.zip", "MA_甲醇/00_主力连续/MA_main_1m.csv"),
    "EG": ("D:/学习资料/DCE_大连商品交易所.zip", "EG_乙二醇/00_主力连续/EG_main_1m.csv"),
}
OUT = "E:/抖音分析/2026-10-16-19-50-44/kline_encoding"

for sym, (zip_path, member) in SRC.items():
    kept, total, bad = 0, 0, 0
    out_path = f"{OUT}/{sym}_2020_1m_raw.csv"
    with zipfile.ZipFile(zip_path) as z, open(out_path, "w", newline="", encoding="utf-8") as fo:
        with z.open(member) as fi:
            header = fi.readline().decode("utf-8-sig").strip()
            fo.write(header + "\n")
            cols = header.split(",")
            i_dt = cols.index("datetime")
            for raw in fi:
                total += 1
                line = raw.decode("utf-8", errors="replace")
                if line[i_dt * 0:].startswith("2020") and line[:4] == "2020":
                    # 快速判定：行首年份
                    fo.write(line if line.endswith("\n") else line + "\n")
                    kept += 1
    print(f"{sym}: total={total:,} kept2020={kept:,} -> {out_path}")
