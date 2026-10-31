# -*- coding: utf-8 -*-
"""统计 bar_60m / bar_30m 中「偏离正确网格」的 bar 占比。

正确网格（60m）应为分钟分量 ∈ {0,15,30}（日盘 10:00/11:15/14:15/15:00，
夜盘整点、残段 02:30 等）；30m 应为 {0,15,30,45}。
偏离 1 分钟（:01/:16/:31/:46 或 :59/:14/:29/:44）即计数因缺失分钟漂移。
"""
from sqlalchemy import text

from app.core.db import session_scope

_SH = __import__("zoneinfo").ZoneInfo("Asia/Shanghai")


def main() -> int:
    with session_scope() as s:
        for tbl, step, allowed in (
            ("bar_60m", 60, {0, 15, 30}),
            ("bar_30m", 30, {0, 15, 30, 45}),
        ):
            total = s.execute(text(f"SELECT count(*) FROM l1_mkt.{tbl}")).scalar()
            # 偏离集合：末位分钟不在 allowed 的都算
            # 但需排除「合法残段」——对 60m，夜盘尾段可能是 02:30（已含）；
            # 30m 残段落在 allowed 内。故直接统计不在 allowed 的。
            off = s.execute(text(f"""
                SELECT count(*) FROM l1_mkt.{tbl}
                WHERE extract(minute FROM (bucket AT TIME ZONE 'Asia/Shanghai'))::int
                      NOT IN ({','.join(str(a) for a in allowed)})
            """)).scalar()
            # 精确统计 +1 漂移（缺失分钟导致）的特征值
            drift_vals = { (a + 1) % 60 for a in allowed } | { (a - 1) % 60 for a in allowed }
            drift = s.execute(text(f"""
                SELECT count(*) FROM l1_mkt.{tbl}
                WHERE extract(minute FROM (bucket AT TIME ZONE 'Asia/Shanghai'))::int
                      IN ({','.join(str(d) for d in drift_vals)})
            """)).scalar()
            print(f"{tbl}: 总 {total:,}  偏离网格 {off:,} ({100*off/total:.3f}%)  "
                  f"其中 ±1 漂移特征 {drift:,} ({100*drift/total:.3f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
