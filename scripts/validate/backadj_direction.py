# -*- coding: utf-8 -*-
"""后复权在期货上的根本性缺陷验证（只读）。

已观察到：RU888 的 roll_segment.cum_offset 范围达 -17,955。
若offset 直接加到价格上，后期段价格必然为负。

本脚本量化三件事：
  A. 各品种 cum_offset 幅度 vs 实际价格水平 → 后期段会不会变负
  B. 若启用 price_shift（整序列常量抬升）能否救回，代价是什么
  C. 前复权(cont_adj 已删) vs 后复权(back_adj) 哪个方向在本市场更可行
"""
import os
import sys

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, r"E:\Docker\qhyc")
sys.argv = ["x"]

PG = dict(host="127.0.0.1", port=5432, user="futures",
          password="qhyc_dev_pwd_2026", dbname="futures")
OUT = r"E:\Docker\qhyc\docs\_swing_out"


def q(sql, **kw):
    with psycopg2.connect(**PG) as cn:
        return pd.read_sql(sql, cn, **kw)


def main():
    # ---- A. cum_offset 幅度 vs 价格水平 ----
    seg = q("""SELECT symbol,
                      min(cum_offset) mn_off, max(cum_offset) mx_off,
                      min(roll_delta) mn_rd, max(roll_delta) mx_rd,
                      count(*) n_seg
               FROM roll_segment WHERE freq='min15' GROUP BY symbol""")
    px = q("""SELECT symbol,
                     min(low) mn_px, max(high) mx_px,
                     (array_agg(close ORDER BY bucket))[1] first_px,
                     (array_agg(close ORDER BY bucket DESC))[1] last_px
              FROM bar_15m WHERE symbol LIKE '%888' AND symbol NOT LIKE '%8888'
              GROUP BY symbol""")
    D = seg.merge(px, on="symbol", how="inner")
    # 后复权后最低价 = 原始最低价 + min(cum_offset)
    D["adj_mn_px"] = D.mn_px + D.mn_off
    D["need_shift"] = (-D.adj_mn_px).clip(lower=0)
    D["shift_pct"] = 100 * D.need_shift / D.first_px
    D["off_span"] = D.mx_off - D.mn_off

    print("=" * 78)
    print("=== A. 后复权把价格推负的程度（min15，84 品种）===")
    print("=" * 78)
    neg = D[D.adj_mn_px <= 0]
    print("  后复权后最低价 ≤0 的品种: %d / %d (%.0f%%)"
          % (len(neg), len(D), 100 * len(neg) / len(D)))
    print("  cum_offset 跨度（max-min）: 中位 %.0f | 最大 %.0f（%s）"
          % (D.off_span.median(), D.off_span.max(),
             D.loc[D.off_span.idxmax(), "symbol"]))
    print()
    print("  最严重的 12 个品种：")
    print(neg.nsmallest(12, "adj_mn_px")[
        ["symbol", "first_px", "last_px", "mn_px", "mn_off", "adj_mn_px",
         "need_shift", "shift_pct"]
    ].to_string(index=False, float_format=lambda v: "%.1f" % v))
    print()

    # ---- B. price_shift 能否救回，代价多大 ----
    print("=" * 78)
    print("=== B. 启用 price_shift（整序列常量抬升）能否救回？===")
    print("=" * 78)
    print("  price_shift 定义（back_adjust.py）：整序列常量抬升，"
          "把最小价抬到 0 以上")
    print("  代价：早期段价格被整体抬高 → 早期\"真实价\"失真")
    print()
    print("  需要 price_shift 的品种: %d 个，需要抬升幅度："
          % (D.need_shift > 0).sum())
    print("    抬升幅度: 中位 %.0f 点 | 最大 %.0f 点（%s）"
          % (D.loc[D.need_shift > 0, "need_shift"].median(),
             D.need_shift.max(),
             D.loc[D.need_shift.idxmax(), "symbol"]))
    print("  抬升幅度占首价比例: 中位 %.0f%% | 最大 %.0f%%"
          % (D.loc[D.need_shift > 0, "shift_pct"].median(),
             D.shift_pct.max()))
    print()
    print("  ⇒ 即便用 price_shift 救回，**早期段的点数已失真**")
    print("    而回测的 ATR 止损/保本全靠点数 → 早期段信号全部偏移")
    print()

    # ---- C. 两个方向的对比 ----
    print("=" * 78)
    print("=== C. 前复权(已删) vs 后复权(待建) 哪个方向可行 ===")
    print("=" * 78)
    print("  【后复权】锚定最早段 → 早期价=真实价，后期价被偏移")
    print("    期货长期趋势向下 ⇒ 累积偏移为负 ⇒ **后期价变负**")
    print("    实测 %d/%d 品种会变负（%.0f%%）"
          % (len(neg), len(D), 100 * len(neg) / len(D)))
    print()
    print("  【前复权】锚定最新段 → 后期价=真实价，早期价被偏移")
    print("    期货换月多为贴水(roll_delta<0) ⇒ 累积偏移为负 ⇒ **早期价变负**")
    print("    实测(已删的 cont_adj) 15/84 品种全负 3.1%%~3.5%%")
    print()
    print("  ⇒ **两个方向在期货上都会把某一段价格压成负数**")
    print("    这不是实现bug，是「期货非永续正价序列」的数学必然")
    print()

    # ---- D. 真正的解：只做等比 ----
    print("=" * 78)
    print("=== D. 第三条路：切真实价 vs 切「相对价」===")
    print("=" * 78)
    print("  两条路：")
    print("   (1) 保持未复权 continuous（现状）——价格真实，但跨换月有断层")
    print("       断层影响：换月点会产生虚假跳空，2×ATR 止损/趋势判断失真")
    print("   (2) 用 back_adj + price_shift —— 断层归零，但早期点数失真")
    print("   (3) ★ 只在「换月段内」用偏移、不做全序列平移 = 等价于 continuous + 换月修补")
    print()

    # 量化：换月断层对 ATR 的实际影响
    D2 = q("""SELECT symbol, bucket, close::float8 c, high::float8 h, low::float8 l
              FROM bar_15m WHERE symbol LIKE '%888' AND symbol NOT LIKE '%8888'
              AND bucket >= '2015-01-01' ORDER BY symbol, bucket""")
    rows = []
    for sym, g in D2.groupby("symbol"):
        g = g[g.l > 0] if (g.l <= 0).any() else g      # 剔除 low=0 的畸形bar
        if len(g) < 1000:
            continue
        c = g.c.values.astype(float)
        if np.any(c <= 0):
            continue
        r = np.abs(np.diff(c) / c[:-1])
        # 断层定义为超过 3 倍日内波动率(占价格比) 的跳空
        atr_ratio = float(((g.h - g.l).rolling(14).mean() / g.c).median())
        if atr_ratio <= 0:
            continue
        jump = r > 3 * atr_ratio
        rows.append(dict(sym=sym, n=len(g), n_jump=int(jump.sum()),
                         pct_jump=100 * jump.mean(), atr_ratio=100 * atr_ratio,
                         max_jump=float(r.max())))
    J = pd.DataFrame(rows)
    print("  换月断层（>3×日内ATR 的单根跳空）在未复权序列里的占比：")
    print("    品种 %d 个 | 平均占比 %.3f%% | 中位 %.3f%%"
          % (len(J), J.pct_jump.mean(), J.pct_jump.median()))
    print("    单根最大跳空: 中位 %.2f%% | 最大 %.2f%%"
          % (100 * J.max_jump.median(), 100 * J.max_jump.max()))
    print()
    print("  ⇒ 若断层占比 <0.1%%，对 ATR/止损的实际影响有限；")
    print("    这是「是否值得为复权做工程投入」的关键判据。")

    D.to_csv(os.path.join(OUT, "backadj_direction_analysis.csv"), index=False)
    J.to_csv(os.path.join(OUT, "rollover_gap_impact.csv"), index=False)
    print()
    print("[out] backadj_direction_analysis.csv / rollover_gap_impact.csv")


if __name__ == "__main__":
    main()
