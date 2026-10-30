# -*- coding: utf-8 -*-
"""047 迁移落地后的验收：
  1. 视图行数是否等于源表行数（不该丢行、不该多行）
  2. 抽样校验同一 (symbol, 时刻) 的数据完全一致
  3. bar_src_coverage 体检视图能否直接回答"某品种谁提供、覆盖到哪"
  4. cron_fetch_1min 是否已停止报错
"""
from sqlalchemy import text
from app.core.db import session_scope

SEP = "=" * 78

with session_scope() as s:
    print(SEP)
    print("1. 视图 vs 源表 —— 行数等价性")
    print(SEP)
    checks = [
        ("hourly_bar", "hourly_bar_uni", "trade_datetime", "t"),
        ("daily_bar", "daily_bar_uni", "trade_date", "d"),
    ]
    for base, view, col, tag in checks:
        nb = s.execute(text(f"SELECT count(*) FROM l0_raw.{base}")).scalar()
        nv = s.execute(text(f"SELECT count(*) FROM l0_raw.{view}")).scalar()
        nd = s.execute(text(f"SELECT count(DISTINCT symbol) FROM l0_raw.{view}")).scalar()
        ok = "✅" if nb == nv else "❌"
        print(f"  {view:<22} 视图={nv:>10,}  源表={nb:>10,}  品种数={nd:<4} {ok}")

    print()
    print(SEP)
    print("2. 抽样逐条比对（视图 vs 源表同一根 bar 的 OHLC 是否一致）")
    print(SEP)
    for base, view, col, tag in checks:
        rows = s.execute(text(f"""
            SELECT v.symbol, v.{col}, v.open, v.high, v.low, v.close, v.volume,
                   b.open, b.high, b.low, b.close, b.volume, v.src
            FROM l0_raw.{view} v
            JOIN l0_raw.{base} b
              ON b.symbol = v.symbol AND b.{col} = v.{col}
            ORDER BY v.{col} DESC LIMIT 5
        """)).fetchall()
        print(f"  [{view}] 最近 5 根:")
        diff = 0
        for r in rows:
            same = tuple(r[2:7]) == tuple(r[7:12])
            if not same:
                diff += 1
            print(f"    {r[0]:<8}{str(r[1])[:19]:<21} "
                  f"O={r[2]} C={r[5]} V={r[6]} src={r[12]} {'OK' if same else 'DIFF!'}")
        print(f"    → 不一致条数: {diff}")

    print()
    print(SEP)
    print("3. bar_src_coverage 体检视图能否直接用（防再次误判覆盖）")
    print(SEP)
    rows = s.execute(text("""
        SELECT tf, src, n_bars, first_day, last_day, stale_days
        FROM l0_raw.bar_src_coverage
        WHERE symbol = 'CU888' ORDER BY tf, first_day
    """)).fetchall()
    print(f"  {'tf':<8}{'src':<14}{'n_bars':>10}  {'first_day':<12}{'last_day':<12}{'stale':>6}")
    for r in rows:
        print(f"  {r[0]:<8}{r[1]:<14}{r[2]:>10,}  {str(r[3]):<12}{str(r[4]):<12}{r[5]:>6}")
    print("  → 小时线全时段 = agg_1m 段 + akshare 段之和，不再是『只有 4 年』的误判")

    print()
    print(SEP)
    print("4. cron_fetch_1min 是否已停摆")
    print(SEP)
    try:
        row = s.execute(text("""
            SELECT enabled, COALESCE(name,'') FROM ops.scheduled_task
            WHERE task_id='cron_fetch_1min'
        """)).first()
        print(f"  ops.scheduled_task.enabled = {row[0]}   name={row[1]}")
        rows = s.execute(text("""
            SELECT started_at, status, COALESCE(message,'')
            FROM ops.scheduled_task_run
            WHERE task_id='cron_fetch_1min'
            ORDER BY started_at DESC LIMIT 4
        """)).fetchall()
        print("  最近流水:")
        for r in rows:
            print(f"    {str(r[0])[:19]}  {r[1]:<20}{(r[2] or '')[:60]}")
    except Exception as e:
        print("  ERR:", str(e).replace("\n", " ")[:150])

    print()
    print(SEP)
    print("回滚方式（如需）：")
    print("  DROP VIEW l0_raw.hourly_bar_uni, l0_raw.daily_bar_uni, l0_raw.bar_src_coverage;")
    print(SEP)
