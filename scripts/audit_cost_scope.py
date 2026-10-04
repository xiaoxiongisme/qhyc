# -*- coding: utf-8 -*-
"""全库档位体检：带 scope 限定的品种，主力/一般合约是否命中正确档位。

同时报告 CLOSE_TODAY 缺口（缺则回测日内会静默回落到平昨）。
用法：python scripts/audit_cost_scope.py
"""
from __future__ import annotations

import sys
from datetime import date

from sqlalchemy import text

from app.core.db import session_scope

sys.stdout.reconfigure(encoding="utf-8")
D = date(2026, 10, 3)

#: 郑交所为 3 位合约码（AP701），其余为 4 位（RB2610）
CZCE = {"AP", "CF", "CJ", "CY", "FG", "JR", "LR", "MA", "OI", "PF", "PK", "PM",
        "PX", "RI", "RM", "RS", "SA", "SF", "SH", "SM", "SR", "TA", "UR", "WH", "ZC"}


def _code(vc: str, mm: str) -> str:
    return "%s%s%s" % (vc, "7" if vc in CZCE else "6", mm)


def main() -> None:
    from app.data.cost import fee_per_lot

    with session_scope() as s:
        vs = [r[0] for r in s.execute(text(
            "SELECT DISTINCT variety_code FROM dim_trading_cost "
            "WHERE effective_to IS NULL AND scope_kind <> 'ALL' "
            "AND instrument_kind = 'FUTURE' ORDER BY 1")).fetchall()]
        print("== 1. 带档位限定的品种 %d 个 ==" % len(vs))
        never = []
        for vc in vs:
            hits, ok = [], False
            # ⚠ 必须包含 06 月：PD/PT(丙烯/PTA) 的档位只对 2606 合约生效，
            #    早期版本只测 01/05/09/10/11/12，误报为「从未命中档位」（假阳性）。
            for mm in ("10", "01", "05", "12", "11", "09", "06", "02", "03", "04",
                       "07", "08"):
                try:
                    tc = fee_per_lot(vc + "888", "OPEN", contract=_code(vc, mm), on_date=D)
                    hits.append("%s=%s/%s" % (mm, tc.scope_kind[:4], tc.fee_value))
                    ok = ok or tc.scope_kind != "ALL"
                except Exception:
                    hits.append("%s=ERR" % mm)
            if not ok:
                never.append(vc)
            print("  %-5s %s%s" % (vc, " ".join(hits), "" if ok else "  <-- 从未命中档位"))
        print("  从未命中档位(%d): %s" % (len(never), never or "无"))

        print()
        print("== 2. CLOSE_TODAY 缺口（回测日内会静默回落平昨） ==")
        gaps = s.execute(text(
            "SELECT c.variety_code FROM dim_trading_cost c "
            "WHERE c.effective_to IS NULL AND c.instrument_kind='FUTURE' "
            "  AND c.scope_kind='ALL' AND c.action='OPEN' "
            "  AND NOT EXISTS (SELECT 1 FROM dim_trading_cost x "
            "    WHERE x.variety_code=c.variety_code AND x.instrument_kind='FUTURE' "
            "      AND x.scope_kind='ALL' AND x.action='CLOSE_TODAY' "
            "      AND x.effective_to IS NULL) "
            "ORDER BY 1")).fetchall()
        codes = [r[0] for r in gaps]
        print("  缺 CLOSE_TODAY 的品种 %d 个: %s" % (len(codes), " ".join(codes) or "无"))

        print()
        print("== 3. 负值/异常费率 ==")
        bad = s.execute(text(
            "SELECT variety_code, action, fee_type, fee_value, broker_markup_value "
            "FROM dim_trading_cost WHERE effective_to IS NULL "
            "  AND (fee_value < 0 OR broker_markup_value < 0 OR exchange_fee_value < 0) "
            "ORDER BY 1")).fetchall()
        print("  负值行 %d 条 %s" % (len(bad), "OK" if not bad else "!!"))
        for r in bad:
            print("    %-6s %-12s %-6s %s / markup=%s" % (r[0], r[1], r[2], r[3], r[4]))

        print()
        print("== 4. 重复有效键 ==")
        dup = s.execute(text(
            "SELECT count(*) FROM (SELECT variety_code, instrument_kind, action, scope_kind "
            "FROM dim_trading_cost WHERE effective_to IS NULL "
            "GROUP BY 1,2,3,4 HAVING count(*) > 1) t")).scalar()
        print("  %s %s" % (dup, "OK" if dup == 0 else "!!"))


if __name__ == "__main__":
    main()
