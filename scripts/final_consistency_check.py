# -*- coding: utf-8 -*-
"""两端一致性终检：迁移 / 成本字典 / 档位 / 换月真源 / 模块①配置。

用法：python scripts/final_consistency_check.py
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
        print("== 1. 迁移 ==")
        rows = [r[0] for r in s.execute(text(
            "SELECT filename FROM schema_migrations ORDER BY filename")).fetchall()]
        print("   已登记 %s 个，末位 %s" % (len(rows), rows[-1] if rows else "-"))

        print()
        print("== 2. 成本字典 ==")
        tot = s.execute(text(
            "SELECT count(*) FROM dim_trading_cost WHERE effective_to IS NULL")).scalar()
        dup = s.execute(text(
            "SELECT count(*) FROM (SELECT variety_code,instrument_kind,action,scope_kind "
            "FROM dim_trading_cost WHERE effective_to IS NULL "
            "GROUP BY 1,2,3,4 HAVING count(*)>1) t")).scalar()
        neg = s.execute(text(
            "SELECT count(*) FROM dim_trading_cost WHERE effective_to IS NULL "
            "AND (fee_value < 0 OR broker_markup_value < 0)")).scalar()
        print("   有效行=%s 重复键=%s 负值=%s" % (tot, dup, neg))
        for r in s.execute(text(
                "SELECT source, count(*) FROM dim_trading_cost WHERE effective_to IS NULL "
                "GROUP BY source ORDER BY 2 DESC")).fetchall():
            print("     %-26s %s" % (r[0], r[1]))

        print()
        print("== 3. 换月真源（G2） ==")
        for r in s.execute(text(
                "SELECT change_source, count(*) AS n, "
                "count(contract_code) FILTER (WHERE contract_code IS NOT NULL) AS c "
                "FROM roll_segment GROUP BY 1 ORDER BY 2 DESC")).fetchall():
            print("   %-22s 段=%-8s 有合约码=%s" % (r[0] or "(未重建)", r[1], r[2]))
        n = s.execute(text(
            "SELECT count(*) FROM anomaly_ticket WHERE field LIKE 'ROLL_%'")).scalar()
        print("   换月校验 anomaly_ticket = %s 条" % n)

        print()
        print("== 4. 模块①配置 ==")
        for t in ("cfg_symbol_margin", "cfg_trading_session", "cfg_holiday",
                  "cfg_feature_switch"):
            try:
                c = s.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                print("   %-22s %s 行" % (t, c))
            except Exception as e:  # noqa: BLE001
                print("   %-22s 缺失/不可读 (%s)" % (t, str(e)[:40]))

        print()
        print("== 5. 主力合约档位命中 ==")
        from app.data.cost import fee_per_lot, round_trip_cost
        for vc in ("RB", "HC", "AU", "MA"):
            try:
                tc = fee_per_lot(vc + "888", "OPEN", contract=vc + "2610", on_date=D)
                print("   %-4s2610 scope=%-11s 值=%s" % (vc, tc.scope_kind, tc.fee_value))
            except Exception as e:  # noqa: BLE001
                print("   %-4s2610 ERR %s" % (vc, str(e)[:50]))
        r = round_trip_cost("RB888", 3072.0, contract="RB2610", on_date=D, lots=1)
        print("   RB2610 端到端往返 = %.2f 元/手" % r["total"])

        print()
        print("== 6. 默认口径（G3） ==")
        from app.data.caliber import default_caliber_for
        for f in ("15m", "daily", "hourly"):
            print("   %-7s -> %s" % (f, default_caliber_for(f, warn=False)))


if __name__ == "__main__":
    main()
