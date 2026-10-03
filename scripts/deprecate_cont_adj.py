# -*- coding: utf-8 -*-
"""废弃现有前复权 cont_adj（含负价数据）。

背景（2026-09-30 实测）
----------------------
现有 `fut_kline(kind='cont_adj')` 是**加法前复权**（锚定最新段），实测：
  * I888 铁矿最低 −1058.5，**51.4% 的 bar 为负**
  * 84 品种中 13 个出现负价（等差前复权方案）
  * 每次新增换月，全历史每根 bar 的复权值都要重写（前复权固有缺陷）
已裁定改用 **等差后复权**（roll_segment），故本批数据废弃。

安全设计
--------
* **默认 dry-run**，只统计不删除。
* 删除需同时给出 `--action delete --confirm`。
* 云端（`--host` 指向 `host.docker.internal`/15432）执行时需要额外 `--i-know-production`。
* 删除前自动做落盘建议提示（pg_dump），不自动执行备份。

用法
----
  python scripts/deprecate_cont_adj.py                      # 本地：只报告
  python scripts/deprecate_cont_adj.py --action delete --confirm
  python scripts/deprecate_cont_adj.py --env cloud --action delete --confirm --i-know-production
"""
import argparse
import os
import sys
import time

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pgconn import add_conn_args, conn_from_args  # noqa: E402

TARGET_KINDS = ('cont_adj',)


def rows_by_freq(cur):
    """逐个 freq 统计。

    刻意**不用**一次性 `GROUP BY freq` 全表扫：fut_kline 是 612 个 chunk 的超表，
    单条聚合要一次锁住全部 chunk（实测在有其他写入并发时会被拖到分钟级超时）。
    按 freq 拆开后每条都能走 `(freq,kind,symbol,trade_datetime)` 主键裁剪。
    """
    out = []
    cur.execute("SELECT DISTINCT freq FROM fut_kline WHERE kind = ANY(%s) ORDER BY 1",
                (list(TARGET_KINDS),))
    freqs = [r[0] for r in cur.fetchall()]
    for f in freqs:
        cur.execute("""
            SELECT count(*),
                   count(*) FILTER (WHERE close <= 0),
                   count(DISTINCT symbol) FILTER (WHERE close <= 0),
                   min(close)
            FROM fut_kline WHERE kind = ANY(%s) AND freq = %s
        """, (list(TARGET_KINDS), f))
        n, n_neg, sym_neg, lo = cur.fetchone()
        out.append((f, n, n_neg, sym_neg, lo))
    return out


def total(cur):
    cur.execute("SELECT count(*) FROM fut_kline WHERE kind = ANY(%s)", (list(TARGET_KINDS),))
    return cur.fetchone()[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--action', choices=['report', 'delete'], default='report')
    ap.add_argument('--kinds', default='cont_adj')
    ap.add_argument('--confirm', action='store_true')
    ap.add_argument('--i-know-production', action='store_true',
                    help='云端删除的二次确认')
    ap.add_argument('--env', default='local', choices=['local', 'cloud'])
    add_conn_args(ap)
    a = ap.parse_args()

    global TARGET_KINDS
    TARGET_KINDS = tuple(k.strip() for k in a.kinds.split(',') if k.strip())

    c = psycopg2.connect(**conn_from_args(a))
    cur = c.cursor()

    print('=' * 84)
    print(f'【{a.env}】废弃目标 kind={TARGET_KINDS} | action={a.action}')
    print('=' * 84)
    print(f"{'freq':<10}{'总行数':>14}{'负价行数':>14}{'负价品种数':>12}{'最低价':>14}")
    tot = 0
    for freq, n, n_neg, sym_neg, lo in rows_by_freq(cur):
        pct = f'{n_neg / n * 100:.1f}%' if n else '-'
        print(f"{freq:<10}{n:>14,}{n_neg:>14,}{sym_neg:>12}"
              f"{(f'{lo:.1f}' if lo is not None else '-'):>14}   负价占比 {pct}")
        tot += n
    print(f"{'合计':<10}{tot:>14,}")

    # 负价品种明细：删除前必须先知道「哪些品种的哪些区间」受影响
    cur.execute("""
        SELECT symbol, freq, count(*) AS n,
               count(*) FILTER (WHERE close <= 0) AS n_neg,
               min(close) AS lo
        FROM fut_kline WHERE kind = ANY(%s) AND close <= 0
        GROUP BY 1, 2 ORDER BY n_neg DESC LIMIT 20
    """, (list(TARGET_KINDS),))
    neg = cur.fetchall()
    if neg:
        print('\n【负价 TOP20 品种×周期】（历史序列已失真，禁止再用）')
        print(f"  {'品种':<12}{'周期':<10}{'总行':>12}{'负价行':>12}{'负价占比':>10}{'最低价':>12}")
        for sym, freq, n, n_neg, lo in neg:
            print(f"  {sym:<12}{freq:<10}{n:>12,}{n_neg:>12,}"
                  f"{n_neg / n * 100:>9.1f}%{lo:>12.1f}")

    if a.action == 'report':
        print('\n[dry-run] 未做任何删除。确认后执行：')
        print(f'  python scripts/deprecate_cont_adj.py --action delete --confirm'
              + (' --i-know-production' if a.env == 'cloud' else ''))
        print('\n建议先做备份：')
        print('  pg_dump -h <host> -U futures -t fut_kline futures > fut_kline_bak.sql')
        c.close()
        return 0

    if not a.confirm:
        print('\n[abort] 删除必须显式 --confirm')
        c.close()
        return 2
    if a.env == 'cloud' and not a.i_know_production:
        print('\n[abort] 云端删除需要额外 --i-know-production（云端数据不可回滚）')
        c.close()
        return 2

    # ★ 2026-10-03：改为**逐 freq 分批删除**。
    # 原实现是单条 DELETE ... WHERE kind=ANY(...)，在 612-chunk 超表上一次性删 994 万行，
    # 实测会长时间挂住（首轮尝试 6 分钟无进展，疑似触发全 chunk 锁扫描）。
    # 本脚本 rows_by_freq() 的注释已给出正解：「按 freq 拆开后每条都能走
    # (freq,kind,symbol,trade_datetime) 主键裁剪」。逐频删除同样可断点续跑
    # （已删频次 rowcount=0，跳过），且单批事务小、不会长时间持锁。
    print('\n[delete] 开始删除（逐 freq 分批）…')
    total = 0
    cur.execute("SELECT DISTINCT freq FROM fut_kline WHERE kind = ANY(%s) ORDER BY 1",
                (list(TARGET_KINDS),))
    del_freqs = [r[0] for r in cur.fetchall()]
    with c:
        for freq in del_freqs:
            t0 = time.time()
            cur.execute(
                "DELETE FROM fut_kline WHERE freq=%s AND kind = ANY(%s)",
                (freq, list(TARGET_KINDS)))
            n = cur.rowcount
            total += max(n, 0)
            print(f'  [delete] {freq:<6} 删除 {max(n,0):>9,} 行'
                  f'（{time.time()-t0:.1f}s，累计 {total:,}）', flush=True)
    print(f'[delete] 已删除 {total:,} 行 kind={TARGET_KINDS}')
    c.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
