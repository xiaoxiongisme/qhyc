# -*- coding: utf-8 -*-
"""输出 l0_raw.hourly_bar / daily_bar 的列结构，供编写 canonical 视图。"""
from sqlalchemy import text
from app.core.db import session_scope

with session_scope() as s:
    for tb in ("hourly_bar", "daily_bar"):
        cols = s.execute(text("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema='l0_raw' AND table_name=:b
            ORDER BY ordinal_position
        """), {"b": tb}).fetchall()
        print("=" * 40, tb)
        for c in cols:
            print(f"    {c[0]:<24} {c[1]:<28} null={c[2]}")
        n = s.execute(text(f"SELECT count(*) FROM l0_raw.{tb}")).scalar()
        print(f"    总行数: {n:,}")
        # 唯一键
        idx = s.execute(text("""
            SELECT indexname, indexdef FROM pg_indexes
            WHERE schemaname='l0_raw' AND tablename=:b ORDER BY indexname
        """), {"b": tb}).fetchall()
        print("    索引:")
        for i in idx:
            print(f"      {i[0]}: {i[1][:150]}")
        print()
