# -*- coding: utf-8 -*-
"""数据库约束体检：列出现有主键/唯一键/外键/索引，并按候选键实测重复行数。

目的（六层解耦 · 数据层）
------------------------
现状盘点（2026-09-28）发现：**40 张业务表零外键、多数无唯一约束**，
重复数据靠「写入时 DELETE 再 COPY」这种应用层约定维持，一旦并发或作业重跑就会
产生重复行，且无任何数据库层拦截。

本脚本只做**只读体检**，不改数据。输出：
  1. 每张表的列、是否超表、现有 PK/UK/FK/索引
  2. 按候选键实测的重复组数 / 重复行数（用于判断能否直接加唯一约束）

候选键来自 CANDIDATE_KEYS（人工按语义登记，见 KEY_NOTE）。

用法
----
  python scripts/db_audit_constraints.py                 # 连 POSTGRES_HOST 默认本地
  python scripts/db_audit_constraints.py --port 15432    # 云端（经 SSH 隧道）
  python scripts/db_audit_constraints.py --only fut_kline,bar_15m
"""
import argparse
import json
import os
import sys

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pgconn import add_conn_args, conn_from_args  # noqa: E402

#: 候选唯一键登记表 —— 人工按语义确认，新增表时在此登记。
#: 超表（TimescaleDB）的唯一键**必须包含分区时间列**，否则建不上。
CANDIDATE_KEYS = {
    # —— 行情层（超表，键必须含时间列）——
    "fut_kline":        ["freq", "kind", "symbol", "trade_datetime"],
    "minute_bar":       ["symbol", "ts"],
    "minute_bar_adj":   ["symbol", "ts"],
    "bar_5m":           ["symbol", "bucket"],
    "bar_15m":          ["symbol", "bucket"],
    "bar_30m":          ["symbol", "bucket"],
    "bar_60m":          ["symbol", "bucket"],
    "hourly_bar":       ["symbol", "trade_datetime", "src"],
    "daily_bar":        ["symbol", "trade_date"],
    "contract_daily":   ["symbol", "trade_date"],
    # —— 基本面 ——
    "spot_basis":       ["symbol", "trade_date"],
    "warehouse_receipt": ["symbol", "trade_date"],
    "member_position_rank": ["symbol", "trade_date", "member", "rank_type"],
    "member_position_rank_summary": ["symbol", "trade_date"],
    "roll_yield":       ["symbol", "trade_date"],
    "inventory":        ["symbol", "trade_date"],
    # —— 因子层 ——
    "factor_registry":  ["factor_id"],
    "factor_value":     ["symbol", "trade_date", "factor_id"],
    # —— 回测 / 预测 / 信号 ——
    "fusion_state_detail": ["symbol", "trade_date"],
    "fusion_signal_log":   ["symbol", "signal_time"],
    "fusion_position":     ["symbol", "trade_date"],
    "backtest_run":    ["run_id"],
    "backtest_trade":  ["run_id", "symbol", "entry_time"],
    # —— 元数据 / 调度 ——
    "symbol_meta":     ["symbol"],
    "task_run":        ["run_id"],
    "sync_state":      ["table_name"],
    "anomaly_ticket":  ["ticket_id"],
}

KEY_NOTE = """
候选键登记原则：
  * 超表（fut_kline/minute_bar/bar_* 等）必须含时间列，否则 TimescaleDB 拒绝；
  * 键以「业务不可重复」为准，不是「查询最频繁」；
  * 未在 CANDIDATE_KEYS 登记的表，本脚本只输出结构不测重复（需人工补登记）。
"""


def q(cur, sql, args=None):
    cur.execute(sql, args or ())
    return cur.fetchall()


def main():
    ap = argparse.ArgumentParser(description="数据库约束只读体检")
    ap.add_argument("--only", default=None, help="只查这些表（逗号分隔）")
    ap.add_argument("--json-out", default=None, help="结果写 JSON")
    ap.add_argument("--no-dup-check", action="store_true", help="跳过重复检测（快）")
    add_conn_args(ap)
    a = ap.parse_args()

    conn = conn_from_args(a)
    c = psycopg2.connect(**conn)
    cur = c.cursor()

    only = set(a.only.split(",")) if a.only else None

    # 1) 表清单（排除系统 schema）
    # 注意：q() 内部已 fetchall，此处必须直接用其返回值，不可再 cur.fetchall()（会得到空）
    tables = q(cur, """
        SELECT c.relname,
               c.relkind,
               (SELECT count(*) FROM pg_constraint pc
                 WHERE pc.conrelid = c.oid AND pc.contype='p') AS has_pk
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m')
        ORDER BY c.relname
    """)
    if only:
        tables = [t for t in tables if t[0] in only]

    # 2) 超表集合
    hypertables = set()
    try:
        hypertables = {r[0] for r in q(cur, "SELECT hypertable_name FROM timescaledb_information.hypertables")}
    except Exception:  # noqa: BLE001
        pass

    # 3) 现有约束/索引
    cons = {}
    for t, ct, d in q(cur, """
        SELECT conrelid::regclass::text, contype,
               pg_get_constraintdef(oid) AS def
        FROM pg_constraint
        WHERE connamespace='public'::regnamespace
    """):
        cons.setdefault(t, []).append((ct, d))

    idxs = {}
    for t, iname, idef in q(cur, """
        SELECT tablename, indexname, indexdef
        FROM pg_indexes WHERE schemaname='public'
    """):
        idxs.setdefault(t, []).append((iname, idef))

    out = []
    print(f"{'表':<34}{'类型':<5}{'超表':<5}{'PK':<4}{'UK':<4}{'FK':<4}{'重复组':<9}{'重复行':<9}")
    print("-" * 84)
    for name, relkind, has_pk in tables:
        if relkind in ("v", "m"):
            print(f"{name:<34}view/")
            continue
        cl = cons.get(name, [])
        n_pk = sum(1 for x in cl if x[0] == "p")
        n_uk = sum(1 for x in cl if x[0] == "u")
        n_fk = sum(1 for x in cl if x[0] == "f")
        dup_g = dup_r = ""
        keys = CANDIDATE_KEYS.get(name)
        if keys and not a.no_dup_check:
            cols = ", ".join(f'"{k}"' for k in keys)
            try:
                r = q(cur, f"""
                    SELECT count(*) AS g, coalesce(sum(cnt-1),0) AS extra FROM (
                      SELECT count(*) AS cnt FROM public.{name}
                      GROUP BY {cols} HAVING count(*)>1
                    ) t
                """)[0]
                dup_g, dup_r = r[0], r[1]
            except Exception as e:  # noqa: BLE001
                dup_g, dup_r = "ERR", str(e)[:20]
                c.rollback()
        print(f"{name:<34}{relkind:<5}{'Y' if name in hypertables else '':<5}"
              f"{n_pk:<4}{n_uk:<4}{n_fk:<4}{str(dup_g):<9}{str(dup_r):<9}")
        out.append(dict(table=name, hypertable=name in hypertables,
                        pk=n_pk, uk=n_uk, fk=n_fk,
                        candidate_keys=keys,
                        dup_groups=dup_g, dup_rows=dup_r,
                        indexes=[x[1] for x in idxs.get(name, [])]))
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"\nJSON 已写 {a.json_out}")
    print(f"\n候选键登记说明:{KEY_NOTE}")
    c.close()


if __name__ == "__main__":
    main()
