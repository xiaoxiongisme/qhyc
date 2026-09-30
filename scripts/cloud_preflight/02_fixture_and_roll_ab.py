# -*- coding: utf-8 -*-
"""云端预检 · L2 换月 A/B 对拍：sp_build_l2_roll_segment (A) vs build_roll_segments.py (B)。

在连接的 STAGING 克隆库上，用受控数据同时跑两侧并对比。默认 synthetic 模式用
测试品种 ZZ888/ZZ8888（插入并清理，绝不污染真实数据）；real 模式对真实品种跑 sp
（仅追加），对比后删除新增段以还原。

已知分歧（如实报告，不假装相等）：
  1) 检测锚点：B 固定 15m 锚定再映射；A 直接在目标 freq 跑双门。
     → 真实数据 30m/60m 换月位置会不一致（B 注释实测 30/60m 段数少 17%）。
       synthetic 干净数据位置一致，无法暴露此分歧，需 --mode real 看 30m/60m。
  2) roll_delta 基准：B 用锚点 gap=open[i]-close[i-1]；A 用 cc=close[i]-close[i-1]。
  3) price_shift：B 用 ceil 正数抬升（仅当 min<=0）；A 用 abs(min cum_offset)。
  4) src_freq：B 恒 min15；A 用 p_freq。

退出码：0=仅已知分歧、min15 位置一致；2=min15 换月位置不一致（双门移植有真 bug）。
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
        brs.apply_positivity(g, segs)
        for s in segs:
            cur.execute("INSERT INTO roll_expected VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (sym, f, s['seg_no'], s['seg_start'], s['roll_delta'],
                         s['cum_offset'], s.get('price_shift', 0.0)))


def compare(cur, sym, freqs, mode, pre_max):
    print("\n" + "=" * 96)
    print(" L2 A/B 对拍判定（A=sp_build_l2_roll_segment / B=build_roll_segments.py）")
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
        sp_pos = set(v[1] for v in sp.values())
        ex_pos = set(v[1] for k, v in ex.items() if mode == 'synthetic' or k > pm)
        pos_match = (sp_pos == ex_pos)
        sp_delta = [sp[k][2] for k in sorted(sp) if (mode == 'synthetic' or k > pm)]
        ex_delta = [ex[k][3] for k in sorted(ex) if (mode == 'synthetic' or k > pm)]
        delta_eq = (len(sp_delta) == len(ex_delta)
                    and all(abs(float(a) - float(b)) < 1e-6 for a, b in zip(sp_delta, ex_delta)))
        sp_ps = set(round(float(v[4]), 2) for v in sp.values())
        ex_ps = set(round(float(v[4]), 2) for v in ex.values())
        print(f"\n[{f}] 段数 A={len(sp)} B={len(ex)} | 位置一致={pos_match} | delta基准一致={delta_eq}")
        print(f"    A price_shift={sp_ps}  B price_shift={ex_ps}")
        if not pos_match:
            if f == 'min15':
                print("    ❌ min15 位置不一致 → 双门移植存在真 bug，退出码 2")
                exit_code = 2
            else:
                print(f"    ⚠ {f} 位置不一致：预期（已知分歧#1 锚点），非 bug")
        if not delta_eq:
            print(f"    ⚠ delta 基准不同（已知分歧#2：A用cc / B用gap），示例 A={sp_delta[:3]} B={ex_delta[:3]}")
    print("\n" + "=" * 96)
    if exit_code == 0:
        print(" 判定：min15 位置一致（双门数学自洽）。其余差异均为「已知分歧」，需产品决策。")
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
