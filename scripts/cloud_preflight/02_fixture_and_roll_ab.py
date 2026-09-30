# -*- coding: utf-8 -*-
"""云端预检 · L2 换月 A/B 对拍：sp_build_l2_roll_segment (A) vs build_roll_segments.py (B)。

在连接的 STAGING 克隆库上，用受控数据同时跑两侧并对比。默认 synthetic 模式用
测试品种 ZZ888/ZZ8888（插入并清理，绝不污染真实数据）；real 模式对真实品种跑 sp
（仅追加），对比后删除新增段以还原。

忠实移植说明（2026-09-30 用户拍板：以 15m 锚定原版为准，不得简化）：
  sp_build_l2_roll_segment 现已**忠实移植** build_roll_segments.py（252/252 验收）：
  固定 15m 锚点、delta 取锚点 gap、双门 |cc|>(amp+1e-6) & |Δ8888|<0.40|cc|、
  src_freq 恒 min15、price_shift 默认 0。两侧为同一算法，A/B 应**逐行相等**。

退出码：0=全部字段逐行相等（含 roll_delta/cum_offset/price_shift）；2=存在不一致（回归）。
"""
import argparse, os, sys, importlib.util
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import psycopg2, psycopg2.extras
import numpy as np, pandas as pd
from pgconn import add_conn_args, conn_from_args

FREQ_TABLE = {'min5': 'bar_5m', 'min15': 'bar_15m', 'min30': 'bar_30m', 'min60': 'bar_60m'}
FREQ_MIN = {'min5': 5, 'min15': 15, 'min30': 30, 'min60': 60}
TEST_SYM, TEST_IDX = 'ZZ888', 'ZZ8888'


def load_brs():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'build_roll_segments.py')
    spec = importlib.util.spec_from_file_location('build_roll_segments', p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def gen_synthetic(cur, sym, idx, roll_at):
    """注入一个干净换月事件：主连 888 在 roll_at 处跳空 +200，指数连 8888 仅动 +5。"""
    base = pd.Timestamp(roll_at)
    for freq, tbl in FREQ_TABLE.items():
        minutes = FREQ_MIN[freq]
        buckets = pd.date_range('2026-03-01 09:00:00+08', '2026-03-31 15:00:00+08', freq=f'{minutes}min')
        buckets = [b for b in buckets if 9 <= b.hour < 15]
        price = 4000.0
        idx_price = 4000.0
        rows = []
        for b in buckets:
            if b == base:
                o, h, l, c = price + 200, price + 205, price + 185, price + 190
                io = ih = il = ic = idx_price + 5.0
                price, idx_price = c, ic
            else:
                o = h = l = c = price
                io = ih = il = ic = idx_price
            rows.append((sym, b.to_pydatetime(), o, h, l, c, 100, 100.0 * price, 1000.0))
            rows.append((idx, b.to_pydatetime(), io, ih, il, ic, 100, 100.0 * idx_price, 1000.0))
        psycopg2.extras.execute_values(
            cur,
            f"INSERT INTO {tbl} (symbol,bucket,open,high,low,close,volume,amount,open_interest) VALUES %s",
            rows)


def compute_expected(cur, brs, sym, freqs):
    cur.execute("""CREATE TEMP TABLE roll_expected (
        symbol text, freq text, seg_no int, seg_start timestamptz,
        exp_delta numeric, exp_cum numeric, exp_shift numeric)""")
    g15 = brs.load_symbol(cur, 'bar_15m', sym)
    if g15 is None or len(g15) < 100:
        raise SystemExit(f'[abort] bar_15m 中 {sym} 不足 100 行，无法锚定')
    idx15 = brs.load_index_close(cur, 'bar_15m', sym, g15)
    if np.isnan(idx15).all():
        raise SystemExit(f'[abort] {sym} 无 8888 指数连')
    events, _, _ = brs.detect_anchor_roll(g15, idx15)
    for f in freqs:
        g = g15 if f == 'min15' else brs.load_symbol(cur, FREQ_TABLE[f], sym)
        if g is None or len(g) < 100:
            print(f'  [skip] {f} 数据不足')
            continue
        _, roll, delta_ev = brs.map_roll_to_freq(g, events, FREQ_MIN[f])
        segs = brs.build_segments(g, roll, delta_ev)
        # 默认不调 apply_positivity（与原版默认不带 --positivity 一致），
        # 使 A/B 在默认 positivity 关闭时 price_shift 均=0，可严格逐行比对。
        for s in segs:
            cur.execute("INSERT INTO roll_expected VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (sym, f, s['seg_no'], s['seg_start'], s['roll_delta'],
                         s['cum_offset'], s.get('price_shift', 0.0)))


def compare(cur, sym, freqs, mode, pre_max):
    print("\n" + "=" * 96)
    print(" L2 A/B 对拍判定（A=sp_build_l2_roll_segment / B=build_roll_segments.py）")
    print("  忠实移植：两侧应逐行相等（默认 positivity 关闭，price_shift 均=0）")
    print("=" * 96)
    exit_code = 0
    for f in freqs:
        cur.execute("SELECT seg_no,seg_start,roll_delta,cum_offset,price_shift FROM roll_segment "
                    "WHERE symbol=%s AND freq=%s ORDER BY seg_no", (sym, f))
        sp = {r[0]: r for r in cur.fetchall()}
        cur.execute("SELECT seg_no,seg_start,exp_delta,exp_cum,exp_shift FROM roll_expected "
                    "WHERE symbol=%s AND freq=%s ORDER BY seg_no", (sym, f))
        ex = {r[0]: r for r in cur.fetchall()}
        pm = pre_max.get(f, -1)
        # 增量模式只对「新增段」严格比对（历史段已由 Python 生成，不在 sp 增量范围内）
        sp_keys = [k for k in sorted(sp) if mode == 'synthetic' or k > pm]
        ex_keys = [k for k in sorted(ex) if mode == 'synthetic' or k > pm]
        rows_eq = True
        diffs = []
        for k in sp_keys:
            if k not in ex:
                rows_eq = False; diffs.append(f"seg_no={k} 仅 A 有"); continue
            a, b = sp[k], ex[k]
            if (abs(float(a[2]) - float(b[3])) > 1e-6 or
                abs(float(a[3]) - float(b[4])) > 1e-6 or
                abs(float(a[4]) - float(b[5])) > 1e-6 or
                a[1] != b[1]):
                rows_eq = False
                diffs.append(f"seg_no={k}: start {a[1]} vs {b[1]} | delta {a[2]} vs {b[3]} "
                             f"| cum {a[3]} vs {b[4]} | shift {a[4]} vs {b[5]}")
        if len(sp_keys) != len(ex_keys):
            rows_eq = False
            diffs.append(f"段数不一致 A={len(sp_keys)} B={len(ex_keys)}")
        print(f"\n[{f}] 比对段数={len(sp_keys)} | 逐行相等={rows_eq}")
        for d in diffs[:10]:
            print("    ❌", d)
        if not rows_eq:
            exit_code = 2
    print("\n" + "=" * 96)
    if exit_code == 0:
        print(" 判定：A/B 逐行完全相等 → 忠实移植校验通过，可退役 Python（保留为基准）。")
    else:
        print(" 判定：存在不一致 → 回归，必须先修 sp，切勿退役 Python。")
    print("=" * 96)
    return exit_code


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--symbol', default=TEST_SYM)
    ap.add_argument('--freqs', default='min15,min30,min60')
    ap.add_argument('--mode', choices=['synthetic', 'real'], default='synthetic')
    ap.add_argument('--roll-at', default='2026-03-15 10:00:00+08')
    add_conn_args(ap)
    a = ap.parse_args()
    sym = a.symbol
    freqs = [x.strip() for x in a.freqs.split(',') if x.strip()]
    brs = load_brs()
    c = psycopg2.connect(**conn_from_args(a))
    cur = c.cursor()
    pre_max = {}
    if a.mode == 'synthetic':
        gen_synthetic(cur, TEST_SYM, TEST_IDX, a.roll_at)
        c.commit()
    else:
        for f in freqs:
            cur.execute("SELECT COALESCE(max(seg_no), -1) FROM roll_segment WHERE symbol=%s AND freq=%s",
                        (sym, f))
            pre_max[f] = cur.fetchone()[0]
    compute_expected(cur, brs, sym, freqs)
    c.commit()
    for f in freqs:
        cur.execute("CALL sp_build_l2_roll_segment(%s)", (f,))
    c.commit()
    rc = compare(cur, sym, freqs, a.mode, pre_max)
    # 清理
    if a.mode == 'synthetic':
        for tbl in FREQ_TABLE.values():
            cur.execute(f"DELETE FROM {tbl} WHERE symbol IN (%s,%s)", (TEST_SYM, TEST_IDX))
        cur.execute("DELETE FROM roll_segment WHERE symbol=%s", (TEST_SYM,))
    else:
        for f in freqs:
            cur.execute("DELETE FROM roll_segment WHERE symbol=%s AND freq=%s AND seg_no>%s",
                        (sym, f, pre_max[f]))
    c.commit()
    c.close()
    sys.exit(rc)


if __name__ == '__main__':
    main()
