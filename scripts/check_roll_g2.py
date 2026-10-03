# -*- coding: utf-8 -*-
"""G2 校验：roll_segment 的 change_source / contract_code 是否正确填充。"""
import sys

from sqlalchemy import text

from app.core.db import session_scope

sys.stdout.reconfigure(encoding="utf-8")

with session_scope() as s:
    print("== 按来源统计 ==")
    for r in s.execute(text(
            "SELECT change_source, count(*), "
            "count(contract_code) FILTER (WHERE contract_code IS NOT NULL) AS with_contract "
            "FROM roll_segment GROUP BY 1 ORDER BY 2 DESC")).fetchall():
        print("   %-22s 段数=%-6s 有合约码=%s" % (r[0] or "(未回填)", r[1], r[2]))

    print()
    print("== MA888 / min15 前 8 段 ==")
    for r in s.execute(text(
            "SELECT seg_no, roll_ts, roll_delta, change_source, contract_code "
            "FROM roll_segment WHERE symbol='MA888' AND freq='min15' "
            "ORDER BY seg_no LIMIT 8")).fetchall():
        print("   seg%-3s ts=%-22s delta=%-10s src=%-20s contract=%s"
              % (r[0], str(r[1])[:22], r[2], r[3], r[4]))

    print()
    print("== anomaly_ticket 中的换月校验记录 ==")
    for r in s.execute(text(
            "SELECT field, count(*) FROM anomaly_ticket "
            "WHERE field LIKE 'ROLL_%' GROUP BY 1")).fetchall():
        print("   %-26s %s" % (r[0], r[1]))
    n = s.execute(text("SELECT count(*) FROM anomaly_ticket WHERE field LIKE 'ROLL_%'")).scalar()
    if n == 0:
        print("   （无 —— 说明双门与 change 点完全一致，或尚未重跑全部品种）")
