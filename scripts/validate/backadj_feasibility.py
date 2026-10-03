# -*- coding: utf-8 -*-
"""back_adj 链路可修复性验证（只读，不改任何数据）。

要回答的核心问题：把 roll_segment.cum_offset 套到 bar_* 上之后，
换月断层是否真的归零、ATR 是否零失真、有无负价。
若三项都过 ⇒ back_adj 链可修复，只需注册调度。
若有任一项不过 ⇒ 修复成本远高于"注册调度"，需重新评估。
"""
import os
import sys
import importlib.util

import numpy as np
import pandas as pd

sys.path.insert(0, r"E:\Docker\qhyc")
sys.argv = ["x"]

import psycopg2  # noqa: E402

PG = dict(host="127.0.0.1", port=5432, user="futures",
          password="qhyc_dev_pwd_2026", dbname="futures")
OUT = r"E:\Docker\qhyc\docs\_swing_out"


def load_bars15():
    sql = ("SELECT symbol, bucket, open::float8, high::float8, low::float8, close::float8 "
           "FROM bar_15m WHERE symbol LIKE '%%888' AND symbol NOT LIKE '%%8888' "
           "AND bucket >= '2015-01-01' ORDER BY symbol, bucket")
    with psycopg2.connect(**PG) as cn:
        df = pd.read_sql(sql, cn, parse_dates=["bucket"])
    out = {}
    for sym, g in df.groupby("symbol"):
        out[sym] = dict(dt=g.bucket.values, o=g.open.values, h=g.high.values,
                        l=g.low.values, c=g.close.values)
    return out


def load_seg(freq="min15"):
    sql = ("SELECT symbol, seg_start, seg_end, roll_ts, roll_delta, cum_offset, price_shift "
           "FROM roll_segment WHERE freq=%s ORDER BY symbol, seg_no")
    with psycopg2.connect(**PG) as cn:
        df = pd.read_sql(sql, cn, params=(freq,),
                         parse_dates=["seg_start", "seg_end", "roll_ts"])
    # ★ 时区口径：bar_*.bucket 是 tz-naive（北京时间），roll_segment.* 是 tz-aware(UTC)。
    #   直接比较会抛 TypeError —— 这正是 app/data/back_adjust.py 也会踩的坑。
    #   统一转 naive（不做时区换算，仅去掉 tz 标记：两者实际都按 UTC 存，
    #   bucket 的 naive 值语义上是 UTC 时钟）。
    for c in ("seg_start", "seg_end", "roll_ts"):
        if getattr(df[c].dt, "tz", None) is not None:
            df[c] = df[c].dt.tz_localize(None)
    return df


def apply_back(b, segs):
    """套用 cum_offset（后复权，锚定最早段：最早段 offset=0）。"""
    dt = pd.to_datetime(pd.Series(b["dt"]))
    if dt.dt.tz is not None:
        dt = dt.dt.tz_localize(None)
    n = len(b["c"])
    off = np.zeros(n)
    for _, s in segs.iterrows():
        m = (dt >= s.seg_start) & (dt <= s.seg_end)
        off[m.values] = float(s.cum_offset or 0.0)
    return b["o"] + off, b["h"] + off, b["l"] + off, b["c"] + off, off


def main():
    bars = load_bars15()
    segs = load_seg("min15")
    seg_by = {s: g for s, g in segs.groupby("symbol")}
    print("bar_15m 品种 %d | roll_segment 品种 %d" % (len(bars), len(seg_by)))
    print()

    rows = []
    for sym in sorted(bars):
        if sym not in seg_by:
            continue
        b = bars[sym]
        if len(b["c"]) < 500:
            continue
        g = seg_by[sym]
        o2, h2, l2, c2, off = apply_back(b, g)

        # ① 换月断层残留：原序列 vs 复权后，在 roll_ts 附近各取窗口最大跳空
        dt = pd.to_datetime(pd.Series(b["dt"]))
        if dt.dt.tz is not None:
            dt = dt.dt.tz_localize(None)
        dts = dt.values
        raw_r = np.abs(np.diff(b["c"]) / np.where(b["c"][:-1] == 0, np.nan, b["c"][:-1]))
        with np.errstate(divide="ignore", invalid="ignore"):
            adj_r = np.abs(np.diff(c2) / np.where(c2[:-1] == 0, np.nan, c2[:-1]))
        adj_r = np.nan_to_num(adj_r, nan=0.0, posinf=0.0, neginf=0.0)

        # 取每个换月点前后各 20 根窗口
        win = []
        for _, s in g.iterrows():
            if pd.isna(s.roll_ts):      # 首个段无换月点
                continue
            k = np.searchsorted(dts, np.datetime64(s.roll_ts))
            if 5 < k < len(dts) - 5:
                win.append((max(k - 20, 0), min(k + 20, len(dts) - 1)))
        if not win:
            continue
        raw_roll = np.mean([raw_r[a:b].max() for a, b in win])
        adj_roll = np.mean([adj_r[a:b].max() for a, b in win])

        # ② 断层抹平比
        smooth = 1.0 - (adj_roll / raw_roll) if raw_roll > 0 else np.nan

        # ③ ATR 失真比
        atr_r = float(pd.Series(b["h"] - b["l"]).rolling(14).mean().median())
        atr_a = float(pd.Series(h2 - l2).rolling(14).mean().median())
        atr_ratio = atr_a / atr_r if atr_r > 0 else np.nan

        # ④ 负价
        neg = int((l2 <= 0).sum())

        # ⑤ 段覆盖率（复权偏移是否真的落到 bar 上）
        n_off = int((np.abs(off) > 1e-9).sum())

        rows.append(dict(sym=sym, n_bars=len(b["c"]), n_seg=len(g),
                         roll_ts_raw=100 * raw_roll, roll_ts_adj=100 * adj_roll,
                         smooth=100 * smooth, atr_ratio=atr_ratio, neg=neg,
                         cov_pct=100.0 * n_off / len(b["c"])))

    D = pd.DataFrame(rows)
    D.to_csv(os.path.join(OUT, "back_adj_feasibility.csv"), index=False)
    print("参与评估品种: %d\n" % len(D))

    print("=== ① 换月断层残留（换月点±20根窗口内最大单根跳空的均值）===")
    print("  原始  平均 %.2f%%" % D.roll_ts_raw.mean())
    print("  复权后 平均 %.2f%%" % D.roll_ts_adj.mean())
    print("  断层抹平率 平均 %.1f%% | 中位 %.1f%% | 最小 %.1f%%"
          % (D.smooth.mean(), D.smooth.median(), D.smooth.min()))
    print("  抹平率 <50%% 的品种数: %d" % (D.smooth < 50).sum())
    print()

    print("=== ② ATR 零失真检查（back_adjust.py 要求：等比会让87.8~90.2%品种失真）===")
    ar = D.atr_ratio
    print("  ATR 比值: 中位 %.4f | 均值 %.4f | 范围 [%.4f, %.4f]"
          % (ar.median(), ar.mean(), ar.min(), ar.max()))
    print("  偏离 ±1%% 以内的品种: %d/%d (%.0f%%)"
          % ((ar.sub(1).abs() < 0.01).sum(), len(ar),
             100 * (ar.sub(1).abs() < 0.01).mean()))
    print()

    print("=== ③ 负价检查 ===")
    print("  有负价的品种: %d 个" % (D.neg > 0).sum())
    if (D.neg > 0).sum():
        print(D[D.neg > 0][["sym", "neg"]].to_string(index=False))
    print()

    print("=== ④ 段覆盖率（复权偏移是否真落到 bar 上）===")
    print("  覆盖率: 中位 %.1f%% | 最小 %.1f%% | <90%% 的品种数 %d"
          % (D.cov_pct.median(), D.cov_pct.min(), (D.cov_pct < 90).sum()))
    print()

    print("=== 最差 8 个品种（按断层抹平率）===")
    print(D.nsmallest(8, "smooth")[
        ["sym", "n_seg", "roll_ts_raw", "roll_ts_adj", "smooth", "atr_ratio", "neg", "cov_pct"]
    ].to_string(index=False))
    print()

    ok = (D.smooth > 80).all() and (D.neg == 0).all() and (
        D.atr_ratio.sub(1).abs() < 0.01).all() and (D.cov > 90).all()
    print("=== 判决 ===")
    print("  断层抹平 >80%%: %s" % ("PASS" if (D.smooth > 80).all() else "FAIL"))
    print("  零负价:        %s" % ("PASS" if (D.neg == 0).all() else "FAIL"))
    print("  ATR 零失真:    %s" % ("PASS" if (D.atr_ratio.sub(1).abs() < 0.01).all() else "FAIL"))
    print("  段覆盖 >90%%:   %s" % ("PASS" if (D.cov_pct > 90).all() else "FAIL"))
    print("  => back_adj 链%s可修复（只需注册调度）"
          % ("**" if ok else "**有条件**"))


if __name__ == "__main__":
    main()
