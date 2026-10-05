# -*- coding: utf-8 -*-
"""云库 bar_5m 脏数据修复：删整行NULL空壳 + 修 low=0。
- 先备份受影响行到 bar_5m_dirty_bak（可回滚）
- 删除分批按年，避免 TimescaleDB 跨所有 chunk 的长事务锁耗尽（用户记忆 §3）
用法：
  python scripts/repair_bar5m_dirty.py              # 仅报告影响范围
  python scripts/repair_bar5m_dirty.py --apply       # 云端执行修复
"""
import argparse
import os
import psycopg2
import sys

sys.stdout.reconfigure(line_buffering=True)


def conn():
    return psycopg2.connect(host=os.getenv("CLOUDPGHOST", "host.docker.internal"),
                            port=int(os.getenv("CLOUDPGPORT", 15432)),
                            user=os.getenv("CLOUDPGUSER", "futures"),
                            password=os.environ["CLOUDPGPWD"],
                            dbname=os.getenv("CLOUDPGDBNAME", "futures"),
                            connect_timeout=10)


TABLE = "bar_5m"
YEARS = list(range(2015, 2027))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='执行修复（默认仅报告）')
    a = ap.parse_args()
    c = conn(); cur = c.cursor()
    # TimescaleDB 大表 DML 解压上限默认 10万行，按月删会超过；会话内设为 unlimited
    cur.execute("SET timescaledb.max_tuples_decompressed_per_dml_transaction = 0")

    cur.execute(f"SELECT count(*) FROM {TABLE} "
                f"WHERE open IS NULL AND high IS NULL AND low IS NULL AND close IS NULL")
    n_shell = cur.fetchone()[0]
    cur.execute(f"SELECT count(*) FROM {TABLE} WHERE low<=0 AND open>0 AND high>0 AND close>0")
    n_low0 = cur.fetchone()[0]
    print(f"[评估] 整行NULL空壳(待删): {n_shell} 行", flush=True)
    print(f"[评估] low=0 其他非0(待修→LEAST): {n_low0} 行", flush=True)

    if not a.apply:
        print("[dry-run] 未修改任何数据。确认后加 --apply", flush=True)
        c.close(); return

    # 1) 备份受影响行（普通表，无需 hypertable 分区）
    cur.execute(f"CREATE TABLE IF NOT EXISTS {TABLE}_dirty_bak AS SELECT * FROM {TABLE} WHERE FALSE")
    cur.execute(f"DELETE FROM {TABLE}_dirty_bak")
    cur.execute(f"INSERT INTO {TABLE}_dirty_bak SELECT * FROM {TABLE} "
                f"WHERE (open IS NULL AND high IS NULL AND low IS NULL AND close IS NULL) OR low<=0")
    c.commit()
    print("[backup] 已备份受影响行到 bar_5m_dirty_bak", flush=True)

    # 2) 分批删空壳（按月，单批解压量可控；配合上面 SET=0 解除 DML 解压上限）
    tot = 0
    for y in range(2015, 2027):
        for m in range(1, 13):
            y0 = f"{y}-{m:02d}-01"
            y1 = f"{y+1}-01-01" if m == 12 else f"{y}-{m+1:02d}-01"
            cur.execute(f"DELETE FROM {TABLE} "
                        f"WHERE open IS NULL AND high IS NULL AND low IS NULL AND close IS NULL "
                        f"AND bucket >= %s AND bucket < %s", (y0, y1))
            n = cur.rowcount
            if n:
                tot += n
                c.commit()
                print(f"  [del] {y}-{m:02d}: {n} 行", flush=True)
    print(f"[done] 删除空壳共 {tot} 行", flush=True)

    # 3) 修 low=0 → LEAST(open, close)（保守下界回填）
    cur.execute(f"UPDATE {TABLE} SET low = LEAST(open, close) "
                f"WHERE low<=0 AND open>0 AND high>0 AND close>0")
    n = cur.rowcount; c.commit()
    print(f"[done] 修复 low=0 共 {n} 行", flush=True)

    cur.execute(f"SELECT count(*) FROM {TABLE} WHERE low IS NULL OR low<=0")
    print(f"[校验] 修复后残留 low NULL/<=0: {cur.fetchone()[0]} 行", flush=True)
    c.close()


if __name__ == '__main__':
    sys.exit(main())
