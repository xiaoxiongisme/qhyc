# -*- coding: utf-8 -*-
"""云端前复权链退役前置诊断（只读）。

目标库：127.0.0.1:15432（SSH 隧道穿透到云端 timescaledb）。
回答：① 前复权表是否仍在被作业写入（max ts 新鲜度）；② 可再生基础是否完整；
③ 是否有视图/函数依赖前复权表。
"""
import sys
import psycopg2

CLOUD = dict(host="127.0.0.1", port=15432, user="futures",
             password="qhyc_dev_pwd_2026", dbname="futures", connect_timeout=8)


def main():
    try:
        c = psycopg2.connect(**CLOUD)
    except Exception as e:
        print("!! 云端连接失败：", e)
        return 2
    cur = c.cursor()

    print("=== ① 云端前复权表最大写入时间（判断生成作业是否已停）===")
    for t, sql in [
        ("minute_bar_adj", "SELECT max(ts), min(ts), count(*) FROM minute_bar_adj"),
        ("fut_kline cont_adj", "SELECT max(trade_datetime), min(trade_datetime), count(*) FROM fut_kline WHERE kind='cont_adj'"),
        ("fut_kline continuous(对照)", "SELECT max(trade_datetime) FROM fut_kline WHERE kind='continuous'"),
    ]:
        try:
            cur.execute(sql)
            r = cur.fetchone()
            print(f"  [{t}] max={r[0]} min={r[1]} n={r[2]:,}")
        except Exception as e:
            print(f"  [{t}] ERR {str(e)[:60]}")

    print("\n=== ② 云端可再生基础完整性 ===")
    for t, sql in [
        ("minute_bar(raw L0)", "SELECT count(*), count(DISTINCT symbol), min(ts), max(ts) FROM minute_bar"),
        ("bar_15m", "SELECT count(*) FROM bar_15m"),
        ("roll_segment 各freq", "SELECT freq, count(*), count(DISTINCT symbol) FROM roll_segment GROUP BY freq ORDER BY freq"),
    ]:
        try:
            cur.execute(sql)
            if "GROUP" in sql:
                for r in cur.fetchall():
                    print(f"  [{t}] {r}")
            else:
                print(f"  [{t}] {cur.fetchone()}")
        except Exception as e:
            print(f"  [{t}] ERR {str(e)[:60]}")

    print("\n=== ③ 视图/函数依赖前复权表（pg_depend）===")
    try:
        cur.execute("""
            SELECT cl.relname, c.relkind
            FROM pg_depend d
            JOIN pg_class c ON c.oid = d.refobjid
            JOIN pg_class cl ON cl.oid = d.objid
            WHERE d.deptype = 'n'
              AND d.refobjid IN (SELECT oid FROM pg_class WHERE relname IN ('minute_bar_adj','cont_adj'))
        """)
        rows = cur.fetchall()
        if rows:
            for obj, kind in rows:
                print(f"  {obj} ({kind})")
        else:
            print("  无依赖（可安全 DROP）")
    except Exception as e:
        print("  ERR", str(e)[:60])

    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
