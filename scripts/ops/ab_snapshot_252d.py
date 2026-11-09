"""CL-20261108-05 / -06 四频统一重算：252 交易日 A/B 基线快照（改动前）。

用途：把最近 252 个交易日（截止 2026-11-07 最后一个完整交易日）的四频
``bar_*_ba`` 与 ``roll_segment`` 落到 ops 快照表，重算后逐根对拍。

为什么必须提前留底：段表改动会改**累计偏移**，历史 ba 被就地覆盖，
没有第二份就无从对比（重算后再跑一遍历史是拿改后的数据跟改后的数据比）。
"""
from app.core.db import get_engine
from sqlalchemy import text

WL = ("A,AO,AP,BR,BU,C,CF,EB,EG,FG,FU,HC,I,JD,JM,L,M,MA,OI,P,PF,PK,PP,PX,"
      "RB,RM,SA,SF,SH,SI,SM,SP,SR,SS,TA,UR,V,Y").split(",")
SYMS = [x + "888" for x in WL]
FREQS = [("min5", "bar_5m_ba"), ("min15", "bar_15m_ba"),
         ("min30", "bar_30m_ba"), ("min60", "bar_60m_ba")]
CUTOFF = "2026-11-07"
WINDOW = 252

eng = get_engine()
with eng.connect() as c:
    days = [r[0] for r in c.execute(text(f"""
        SELECT DISTINCT (bucket AT TIME ZONE 'Asia/Shanghai')::date AS d
          FROM l1_mkt.bar_60m
         WHERE bucket <= '{CUTOFF} 23:59:59+08'
         ORDER BY d DESC LIMIT {WINDOW}""")).fetchall()]
start, end = days[-1], days[0]
print(f"窗口 {len(days)} 个交易日：{start} ~ {end}")

with eng.begin() as c:
    c.execute(text("SET LOCAL max_parallel_workers_per_gather = 0"))
    c.execute(text("SET LOCAL jit = off"))
    for freq, tbl in FREQS:
        bak = f"ops.ba_ab_before_{freq}"
        c.execute(text(f"DROP TABLE IF EXISTS {bak}"))
        c.execute(text(f"""
            CREATE TABLE {bak} AS
            SELECT symbol, bucket, open, high, low, close, volume, adj_offset
              FROM l2_adj.{tbl}
             WHERE symbol = ANY(:a)
               AND bucket >= :st"""), {"a": SYMS, "st": f"{start} 00:00:00+08"})
        print(f"  {bak}: {c.execute(text(f'SELECT count(*) FROM {bak}')).scalar()} 行")

    c.execute(text("DROP TABLE IF EXISTS ops.roll_segment_bak_20261109_all4"))
    c.execute(text("""CREATE TABLE ops.roll_segment_bak_20261109_all4 AS
                      SELECT * FROM l2_adj.roll_segment WHERE symbol = ANY(:a)"""),
              {"a": SYMS})
    n = c.execute(text("SELECT count(*), count(DISTINCT freq) FROM ops.roll_segment_bak_20261109_all4")).fetchone()
    print(f"  ops.roll_segment_bak_20261109_all4: {n[0]} 段 / {n[1]} 个频率")

    c.execute(text("DROP TABLE IF EXISTS ops.ab_window_20261109"))
    c.execute(text("""CREATE TABLE ops.ab_window_20261109 AS
                      SELECT :st::date AS win_start, :en::date AS win_end, :n::int AS n_days"""),
              {"st": str(start), "en": str(end), "n": len(days)})
print("SNAPSHOT DONE")
