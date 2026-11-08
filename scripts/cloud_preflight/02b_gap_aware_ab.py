# -*- coding: utf-8 -*-
"""CL-20261107-15 专项 A/B：换月事件落在**跨休市空隙**时的映射。

为什么需要这个脚本（2026-11-08 实测，CL-20261108-04）
------------------------------------------------------
`02_fixture_and_roll_ab.py` 只剩 `cpython-312.pyc`，且有两个坑：

1. `load_brs()` 用 `os.path.join(dirname(abspath(__file__)), '..',
   'build_roll_segments.py')` 定位基准文件。pyc 在 `__pycache__/` 下，
   该路径指向 `cloud_preflight/build_roll_segments.py`（不存在）→
   `main()` 一跑就崩；`--help` 能出只是因为 `parse_args` 提前退出。
2. `gen_synthetic()` 的 buckets 过滤条件为 `9 <= hour < 15`，**只造日盘
   连续网格**。所以 `--roll-at` 无论给 21:15 还是别的时间，都造不出夜盘
   换月事件，也就复现不了「15:00 桶被撑成 7 小时」的缺陷。

本脚本自包含：直接加载仓库的 `scripts/build_roll_segments.py` 算期望值，
自己造带休市空隙的 fixture，不依赖 pyc。

安全红线（同目录 README）
------------------------
只在 STAGING 库执行。默认拒绝 `dbname='futures'`；测试品种 ZZ888/ZZ8888
全程先 DELETE 再 INSERT，不碰任何真实数据。

用法（生产宿主）
--------------
  set -a; . /home/ubuntu/qhyc_work/qhyc.env; set +a
  PYTHONPATH=/Docker/qhyc \
    /home/ubuntu/qhyc_work/venv/bin/python \
    /Docker/qhyc/scripts/cloud_preflight/02b_gap_aware_ab.py --db qhyc_staging

fixture：31 天，15m/30m/60m 各有日盘（09:00–15:00）与夜盘（21:00 起）。
60m 网格日盘末根 15:00、夜盘首根 22:00 —— 精确复现 PF888 2026-11-04
（21:15 真换月 gap=−240，被 054 归到 15:00）。主连跳 +200、指数连仅 +5，
满足双门 `|cc|>(amp+1e-6)` 且 `|Δ8888|<0.40|cc|`。
"""
import argparse
import importlib.util
import os
import sys
from pathlib import Path

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

SYM, IDX = "ZZ888", "ZZ8888"
FREQ_TABLE = {"min15": "bar_15m", "min30": "bar_30m", "min60": "bar_60m"}
FREQ_MIN = {"min15": 15, "min30": 30, "min60": 60}
ROLL_AT = pd.Timestamp("2026-03-15 21:15:00+08")


def load_brs(path):
    p = Path(path)
    spec = importlib.util.spec_from_file_location("brs_gap_ab", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def build_grid():
    days = pd.date_range("2026-03-01", "2026-03-31", freq="D", tz="Asia/Shanghai")
    g15, g30, g60 = [], [], []
    for d in days:
        g15 += list(pd.date_range(d + pd.Timedelta(hours=9),
                                 d + pd.Timedelta(hours=14, minutes=45), freq="15min"))
        g15 += list(pd.date_range(d + pd.Timedelta(hours=21),
                                 d + pd.Timedelta(hours=22, minutes=45), freq="15min"))
        g30 += list(pd.date_range(d + pd.Timedelta(hours=9),
                                 d + pd.Timedelta(hours=14, minutes=30), freq="30min"))
        g30 += list(pd.date_range(d + pd.Timedelta(hours=21),
                                 d + pd.Timedelta(hours=22, minutes=30), freq="30min"))
        for h in (9, 10, 11, 13, 14, 15, 22, 23):
            g60.append(d + pd.Timedelta(hours=h))
    return {"bar_15m": g15, "bar_30m": g30, "bar_60m": g60}


def price_row(sym, b, is_main):
    base = 4000.0
    if b >= ROLL_AT:
        base += 200.0 if is_main else 5.0
    return (sym, b, base, base, base, base, 1, base, 0.0)


def norm_ts(x):
    return pd.Timestamp(x).tz_convert("Asia/Shanghai").isoformat()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="qhyc_staging")
    ap.add_argument("--freqs", default="min15,min30,min60")
    ap.add_argument("--brs-path", default=str(REPO / "scripts" / "build_roll_segments.py"),
                    help="Python 基准文件路径（可用 git show 导出旧版作对照）")
    ap.add_argument("--call-symbol", default="",
                    help="非空则走 055 三参 CALL(freq,false,symbol)，验证 p_symbol 收窄")
    a = ap.parse_args()

    if a.db == "futures":
        print("[abort] 拒绝连生产库 futures；harness 只在 STAGING 跑", file=sys.stderr)
        return 2

    freqs = [x.strip() for x in a.freqs.split(",") if x.strip()]
    brs = load_brs(a.brs_path)
    conn = psycopg2.connect(host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
                            port=int(os.getenv("POSTGRES_PORT", "5432")),
                            dbname=a.db, user=os.getenv("POSTGRES_USER", "futures"),
                            password=os.getenv("POSTGRES_PASSWORD", ""))
    cur = conn.cursor()

    # 清理（含 bar_5m，保证不残留上一次的测试品种）
    for t in set(FREQ_TABLE.values()) | {"bar_5m"}:
        cur.execute("DELETE FROM %s WHERE symbol IN (%%s,%%s)" % t, (SYM, IDX))
    cur.execute("DELETE FROM roll_segment WHERE symbol IN (%s,%s)", (SYM, IDX))
    conn.commit()

    grid = build_grid()
    for tbl, buckets in grid.items():
        rows = ([price_row(SYM, b, True) for b in buckets]
                + [price_row(IDX, b, False) for b in buckets])
        execute_values(
            cur,
            "INSERT INTO %s (symbol,bucket,open,high,low,close,volume,amount,open_interest) "
            "VALUES %%s" % tbl, rows, page_size=1000)
    conn.commit()
    print("[fixture] %s | 换月锚点=%s" %
          ({k: len(v) for k, v in grid.items()}, ROLL_AT))

    # B 侧：15m 锚点 → 各 freq 映射 → 段
    g15 = brs.load_symbol(cur, "bar_15m", SYM)
    if g15 is None or len(g15) < 100:
        print("[abort] bar_15m 锚点不足 100 行", file=sys.stderr)
        return 2
    idx15 = brs.load_index_close(cur, "bar_15m", SYM, g15)
    events = brs.detect_anchor_roll(g15, idx15)[0]
    exp = {}
    for f in freqs:
        g = g15 if f == "min15" else brs.load_symbol(cur, FREQ_TABLE[f], SYM)
        _, roll, delta_ev = brs.map_roll_to_freq(g, events, FREQ_MIN[f])
        exp[f] = brs.build_segments(g, roll, delta_ev)

    # A 侧：调用当前生效的 SP
    for f in freqs:
        if a.call_symbol:
            cur.execute("CALL sp_build_l2_roll_segment(%s, false, %s)", (f, a.call_symbol))
        else:
            cur.execute("CALL sp_build_l2_roll_segment(%s)", (f,))
    conn.commit()

    rc, diffs = 0, []
    for f in freqs:
        cur.execute(
            "SELECT seg_no,seg_start,roll_delta,cum_offset,price_shift,n_bars "
            "FROM roll_segment WHERE symbol=%s AND freq=%s ORDER BY seg_no", (SYM, f))
        got = cur.fetchall()
        want = exp[f]
        if len(got) != len(want):
            rc = 2
            diffs.append("[%s] 段数 A=%d B=%d" % (f, len(got), len(want)))
            continue
        for arow, wrow in zip(got, want):
            if (arow[0] != wrow["seg_no"]
                    or norm_ts(arow[1]) != norm_ts(wrow["seg_start"])
                    or abs(float(arow[2]) - float(wrow["roll_delta"])) > 1e-6
                    or abs(float(arow[3]) - float(wrow["cum_offset"])) > 1e-6
                    or abs(float(arow[4]) - float(wrow.get("price_shift", 0.0))) > 1e-6):
                rc = 2
                diffs.append("[%s] seg_no=%s A=%s B=%s" %
                             (f, arow[0], arow[1:5], dict(wrow)))
        seg1 = [r for r in got if r[0] == 1]
        mark = ("seg1=%s n_bars=%s" % (seg1[0][1], seg1[0][5])) if seg1 else "seg1 缺失"
        print("[%s] 段数=%d 逐行=%s %s" %
              (f, len(got), "相等" if not any(d.startswith("[%s]" % f) for d in diffs) else "不等",
               mark))
    for d in diffs:
        print("  ❌", d)
    print("判定：%s（brs=%s db=%s）" %
          ("A/B 逐行相等 → 跨休市映射修正生效" if rc == 0 else "存在不一致 → 回归",
           a.brs_path, a.db))
    conn.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
