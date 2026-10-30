from app.core.db import session_scope
from app.core.logging import setup_logging
from app.ingest.synthesizer import synthesize_bars_incremental
from sqlalchemy import text

setup_logging()
with session_scope() as s:
    st = synthesize_bars_incremental(s, days_back=2)
    print("stats:", st)

with session_scope() as s:
    print("\n=== 增量后形态抽验（RB888 / SC888 最近 5 天，bar_60m）===")
    rows = s.execute(text("""
        SELECT symbol, (bucket AT TIME ZONE 'Asia/Shanghai')::date d,
               string_agg(to_char(bucket AT TIME ZONE 'Asia/Shanghai','HH24:MI'),' '
                          ORDER BY bucket), count(*)
        FROM l1_mkt.bar_60m
        WHERE symbol IN ('RB888','SC888')
          AND (bucket AT TIME ZONE 'Asia/Shanghai')::date >= CURRENT_DATE - 6
        GROUP BY 1,2 ORDER BY 1,2
    """)).fetchall()
    for r in rows:
        print(f"  {r[0]:<8}{r[1]}  n={r[3]:>2}  {r[2]}")
