# -*- coding: utf-8 -*-
"""成本字典体检：来源分布 / 重复键 / 档位命中 / 端到端成本。

用法：python scripts/verify_cost_dict.py
"""
from __future__ import annotations

import sys
from datetime import date

from sqlalchemy import text

from app.core.db import session_scope

sys.stdout.reconfigure(encoding="utf-8")
D = date(2026, 10, 3)


def main() -> None:
    with session_scope() as s:
        print("== 1. 来源分布（有效行） ==")
        tot = 0
        for src, n in s.execute(text(
                "SELECT source, count(*) FROM dim_trading_cost "
                "WHERE effective_to IS NULL GROUP BY source ORDER BY 2 DESC")).fetchall():
            print("   %-26s %s" % (src, n))
            tot += n
        print("   有效行合计 =", tot)

        dup = s.execute(text(
            "SELECT count(*) FROM (SELECT variety_code, instrument_kind, action, scope_kind "
            "FROM dim_trading_cost WHERE effective_to IS NULL "
            "GROUP BY 1,2,3,4 HAVING count(*) > 1) t")).scalar()
        print("   重复有效键 =", dup, "OK" if dup == 0 else "!! 需修")

        print()
        print("== 2. akshare 来源残留（应为兜底缺口，非覆盖官方） ==")
        for r in s.execute(text(
                "SELECT variety_code, action, fee_type, fee_value FROM dim_trading_cost "
                "WHERE effective_to IS NULL AND source = 'akshare_fees_info' "
                "ORDER BY variety_code LIMIT 40")).fetchall():
            print("   %-6s %-12s %-6s %s" % (r[0], r[1], r[2], r[3]))

    print()
    print("== 3. 档位命中（主力 vs 一般合约） ==")
    from app.data.cost import fee_per_lot
    for vc, mm in (("RB", "2610"), ("RB", "2609"), ("HC", "2610"), ("HC", "2609"),
                   ("AU", "2612"), ("I", "2609"), ("MA", "2701")):
        c = "%s%s" % (vc, mm)
        try:
            tc = fee_per_lot(vc + "888", "OPEN", contract=c, on_date=D)
            print("   %-8s scope=%-10s ‱/元=%s" % (c, tc.scope_kind, tc.fee_value))
        except Exception as e:
            print("   %-8s ERROR %s" % (c, str(e)[:60]))

    print()
    print("== 4. 端到端往返成本（1 手，含 1 跳/边滑点） ==")
    from app.data.cost import round_trip_cost
    for sym, px, c in (("RB888", 3072.0, "RB2610"), ("RB888", 3072.0, "RB2609"),
                       ("AU888", 900.9, "AU2612"), ("MA888", 3850.0, "MA2701"),
                       ("BZ888", 6231.0, None), ("PL888", 6671.0, None)):
        try:
            a = round_trip_cost(sym, px, contract=c, on_date=D, lots=1,
                                close_action="CLOSE_TODAY")
            b = round_trip_cost(sym, px, contract=c, on_date=D, lots=1,
                                close_action="CLOSE_YEST")
            print("   %-7s %-8s 日内=%-8.2f 隔夜=%-8.2f" % (sym, c or "-", a["total"], b["total"]))
        except Exception as e:
            print("   %-7s %-8s ERROR %s" % (sym, c or "-", str(e)[:60]))


if __name__ == "__main__":
    main()
