# -*- coding: utf-8 -*-
"""修复 Bar 表里 low=0 的脏数据（采集源缺陷，非复权造成）。

背景（2026-09-30 实测）
----------------------
6 个品种共 **9 根** bar 的 `low = 0`，且三张表（bar_15m/bar_30m/bar_60m）
加云端 bar_5m 都是**同一批 9 根** ⇒ 污染来自采集源。
已追到最小粒度：云端 `minute_bar` 里 FB888 2022-01-13 09:02 这根 1 分钟 bar 本身
就是 `open=1293 / high=1293.5 / low=0 / close=1293` —— low 没被正确初始化。

分布规律：**全部落在时段开盘第一根**（09:00 六根、21:00 三根）。

为什么必须修
------------
`high - low` 会从正常的约 4 点被撑到 1295 点 ⇒ 该处 ATR 暴涨到上百倍，
并污染后续 ~14 根 bar 的窗口内的 ATR / MA / 止损距离。
这些恰好是全仓库 ATR 类指标的隐性污染源，且**不报错**。

修复口径
--------
`low := LEAST(open, close)`。理由：真实 low 未知，取已知价格中最小的一个是**保守下界**
（真实的 low 只会 ≤ 它），既消除 0 值，又不会人为抬高振幅。
不做 `low := min(open, close, high)` 是因为 high ≥ open/close，取它没意义。

用法
----
  python scripts/repair_zero_low.py                        # 只报告（默认）
  python scripts/repair_zero_low.py --apply                # 本地修复
  python scripts/repair_zero_low.py --apply --tables minute_bar \\
         --host host.docker.internal --port 15432          # 修源头（云端，谨慎）
"""
import argparse
import os
import sys

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_roll_segments import TABLE  # noqa: E402

CONN = dict(host=os.getenv("POSTGRES_HOST", "timescaledb"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            user=os.getenv("POSTGRES_USER", "futures"),
            password=os.environ["POSTGRES_PASSWORD"],
            dbname=os.getenv("POSTGRES_DB", "futures"))

ALL_TABLES = ['bar_5m', 'bar_15m', 'bar_30m', 'bar_60m', 'minute_bar']


def scan(cur, table):
    cur.execute(f"""
        SELECT symbol, bucket, open, high, low, close
        FROM {table} WHERE low <= 0 ORDER BY symbol, bucket
    """)
    return cur.fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tables', default=','.join(TABLE[f] for f in ('min5', 'min15', 'min30', 'min60')))
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--no-backup', action='store_true', help='不做修复前快照（默认会做）')
    a = ap.parse_args()
    tables = [t.strip() for t in a.tables.split(',') if t.strip()]

    c = psycopg2.connect(**CONN)
    cur = c.cursor()
    total_bad = 0
    found = {}
    for t in tables:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (t,))
        if not cur.fetchone()[0]:
            print(f'  {t:<12} 表不存在，跳过')
            continue
        rows = scan(cur, t)
        found[t] = rows
        total_bad += len(rows)
        print(f'  {t:<12} low<=0 共 {len(rows):>4} 根')

    print('\n' + '=' * 92)
    print('【明细】')
    print('=' * 92)
    print(f"{'表':<12}{'品种':<10}{'时间':<22}{'open':>10}{'high':>10}{'low':>8}{'close':>10}{'修复后low':>12}")
    for t, rows in found.items():
        for sym, b, o, h, l, cl in rows:
            fixed = min(float(o), float(cl))
            print(f"{t:<12}{sym:<10}{str(b)[:19]:<22}{float(o):>10.1f}{float(h):>10.1f}"
                  f"{float(l):>8.1f}{float(cl):>10.1f}{fixed:>12.1f}")
    print(f'\n合计 {total_bad} 根')

    if not a.apply:
        print('\n[dry-run] 未修改任何数据。确认后执行：')
        print(f'  python scripts/repair_zero_low.py --apply --tables {a.tables}')
        c.close()
        return 0

    # 修复前快照：存到 *_bad_low 备份表，可随时回滚
    if not a.no_backup:
        for t, rows in found.items():
            if not rows:
                continue
            bak = f'{t}_bad_low_bak'
            cur.execute(f"CREATE TABLE IF NOT EXISTS {bak} AS SELECT * FROM {t} WHERE FALSE")
            cur.execute(f"DELETE FROM {bak}")
            cur.execute(
                f"INSERT INTO {bak} SELECT * FROM {t} WHERE low <= 0")
        c.commit()
        print(f'\n[backup] 已把受影响行备份到 *_bad_low_bak 表')

    n_total = 0
    for t, rows in found.items():
        if not rows:
            continue
        # 只修「low<=0 且 high>0」的行；high 也为 0 的说明整根无数据，不动（另行处理）
        cur.execute(
            f"UPDATE {t} SET low = LEAST(open, close) "
            f"WHERE low <= 0 AND high > 0 AND open > 0 AND close > 0")
        n = cur.rowcount
        n_total += n
        print(f'  [fix] {t:<12} 修复 {n} 根')
    c.commit()
    print(f'\n[done] 共修复 {n_total} 根 low=0 → LEAST(open, close)')
    c.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
