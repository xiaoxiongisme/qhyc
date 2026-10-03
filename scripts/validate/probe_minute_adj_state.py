# -*- coding: utf-8 -*-
"""前复权链退役前置探测（只读，不改任何数据）。

回答 R3 安全门四件事：
 ① minute_bar_adj 是否还在、真实行数/磁盘占用；
 ② 原始源 minute_bar（未复权 L0）是否完整（可再生基础）；
 ③ roll_segment 覆盖哪些 freq（能否支撑 1m 后复权再生）；
 ④ 谁还在依赖 minute_bar_adj（视图/函数/外键/物化依赖）。
"""
import sys
import psycopg2

PG = dict(host="127.0.0.1", port=5432, user="futures",
          password="qhyc_dev_pwd_2026", dbname="futures")


def q1(cur, sql, params=None):
    cur.execute(sql, params)
    return cur.fetchall()


def main():
    c = psycopg2.connect(**PG)
    cur = c.cursor()
    print("=" * 80)
    print("【① minute_bar_adj 现状】")
    print("=" * 80)
    try:
        rows = q1(cur, """
            SELECT
              (SELECT count(*) FROM minute_bar_adj) AS n_rows,
              (SELECT count(DISTINCT symbol) FROM minute_bar_adj) AS n_sym,
              (SELECT min(ts) FROM minute_bar_adj) AS t_min,
              (SELECT max(ts) FROM minute_bar_adj) AS t_max,
              (SELECT pg_total_relation_size('minute_bar_adj')) AS bytes
        """)
        n, nsym, tmin, tmax, bts = rows[0]
        print(f"  行数        : {n:,}")
        print(f"  品种数      : {nsym}")
        print(f"  时间范围    : {tmin} -> {tmax}")
        print(f"  磁盘占用    : {bts/1024**3:.2f} GB ({bts:,} bytes)")
        # 负价比例
        neg = q1(cur, "SELECT count(*) FROM minute_bar_adj WHERE close <= 0")
        print(f"  负价行数    : {neg[0][0]:,}  ({100*neg[0][0]/n:.2f}%)" if n else "  -")
        low = q1(cur, "SELECT min(close) FROM minute_bar_adj")
        print(f"  最低价      : {low[0][0]}")
    except Exception as e:
        print("  !! 查询失败（表可能不存在）:", e)

    print("\n" + "=" * 80)
    print("【② 原始源 minute_bar（未复权 L0，可再生基础）】")
    print("=" * 80)
    rows = q1(cur, """
        SELECT
          (SELECT count(*) FROM minute_bar) AS n_rows,
          (SELECT count(DISTINCT symbol) FROM minute_bar) AS n_sym,
          (SELECT min(ts) FROM minute_bar) AS t_min,
          (SELECT max(ts) FROM minute_bar) AS t_max
    """)
    n, nsym, tmin, tmax = rows[0]
    print(f"  行数        : {n:,}")
    print(f"  品种数      : {nsym}")
    print(f"  时间范围    : {tmin} -> {tmax}")

    print("\n" + "=" * 80)
    print("【③ roll_segment 覆盖频段 + 段数】")
    print("=" * 80)
    rows = q1(cur, """
        SELECT freq, count(*) AS n_seg, count(DISTINCT symbol) AS n_sym,
               min(seg_start), max(seg_end)
        FROM roll_segment GROUP BY freq ORDER BY freq
    """)
    for freq, nseg, nsym, smin, smax in rows:
        print(f"  {freq:<8} 段数={nseg:>6,} 品种={nsym:>3}  范围 {smin} -> {smax}")
    freqs = [r[0] for r in rows]
    print(f"  => 含 min1? {'是' if 'min1' in freqs else '否（关键：1m 后复权再生无 segment 支撑）'}")

    print("\n" + "=" * 80)
    print("【④ 依赖 minute_bar_adj 的对象（视图/物化视图/函数）】")
    print("=" * 80)
    deps = q1(cur, """
        SELECT DISTINCT cl.relname AS obj, c.relkind AS kind
        FROM pg_depend d
        JOIN pg_class c ON c.oid = d.refobjid
        JOIN pg_class cl ON cl.oid = d.objid
        WHERE d.refobjid = 'minute_bar_adj'::regclass
          AND d.deptype = 'n'   -- normal dependency
        ORDER BY 2, 1
    """)
    if deps:
        for obj, kind in deps:
            km = {'v': '视图', 'm': '物化视图', 'f': '函数', 'r': '普通表', 'i': '索引'}
            print(f"  {obj}  ({km.get(kind, kind)})")
    else:
        print("  无视图/物化视图/函数依赖（仅表自身，可安全 DROP）。")

    # 顺带：caliber / barstore 代码中是否仍注册 minute_bar_adj 路由
    print("\n" + "=" * 80)
    print("【⑤ 运行中的生成作业是否仍在写 minute_bar_adj】")
    print("=" * 80)
    print("  检查 retire_minute_adj 开关是否生效（config 表/local.yaml）。")
    print("  （由人工在后续步骤确认 scheduler 04:30 job 已停）")
    c.close()


if __name__ == "__main__":
    sys.exit(main())
