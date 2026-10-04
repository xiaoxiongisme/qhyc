# -*- coding: utf-8 -*-
"""修复「期货 ALL 档被写成 FREE(零成本)」的静默失效 + 落地锡无分档裁定。

2026-10-04 审计发现：18 个品种的 ``dim_trading_cost`` 期货 ALL/OPEN 是 ``FREE 0``
（成本按 0 计），而**权威种子 CSV 里全都有正确费率**（AD 0.5permille / ZN FIXED 3 /
SC FIXED 20 …）。根因指向 ``build_cost_dict.py`` 的兜底分支
「未识别费率原文 -> ('FREE', 0.0) 以免编造」—— 解析失败即**静默归零**，
正是本项目最典型的静默失效模式（见 memory）。

处置
----
1. 以**种子 CSV 为权威**重算「DB=FREE 而 CSV 非 FREE」的期货 ALL 档行；
2. 落地用户 2026-10-04 裁定「锡固定 3 元/手、无分档」，闭合其冗余 CONTRACTS 档。

用法：``python scripts/repair_free_fee.py [--apply]``
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

from sqlalchemy import text

from app.core.db import session_scope

sys.stdout.reconfigure(encoding="utf-8")

CSV = os.path.join("db", "seed", "dim_trading_cost_seed.csv")
#: 用户 2026-10-04 确认：锡固定 3 元/手、无分档
SN_UNTIERED = ["SN"]


def load_csv() -> dict:
    out: dict = {}
    if not os.path.exists(CSV):
        print("[abort] 找不到种子 CSV:", CSV)
        return out
    with open(CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("instrument_kind") != "FUTURE" or r.get("scope_kind") != "ALL":
                continue
            if r.get("action") not in ("OPEN", "CLOSE_YEST"):
                continue
            v = (r.get("variety_code") or "").upper()
            if v:
                out[(v, r["action"])] = (r["fee_type"], r["fee_value"], r.get("note") or "")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    ref = load_csv()
    print("[input] 种子 CSV 权威费率 %d 条（期货 ALL 档 OPEN/CLOSE_YEST）" % len(ref))

    plan, closable = [], []
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT id, variety_code, action, fee_type, fee_value "
            "FROM dim_trading_cost WHERE instrument_kind='FUTURE' AND scope_kind='ALL' "
            "AND action IN ('OPEN','CLOSE_YEST') AND effective_to IS NULL "
            "AND (fee_type='FREE' OR fee_value = 0)")).fetchall()
        for rid, v, act, ft, fv in rows:
            key = (v.upper(), act)
            if key not in ref:
                print("   [keep] %-6s%-12s CSV 亦无值 -> 保持 FREE（需人工补）" % (v, act))
                continue
            cft, cfv, cnote = ref[key]
            if cft == "FREE" or float(cfv or 0) == 0:
                continue
            plan.append((rid, v, act, ft, fv, cft, float(cfv), cnote))
        for v in SN_UNTIERED:
            n = s.execute(text(
                "SELECT count(*) FROM dim_trading_cost WHERE variety_code=:v "
                "AND instrument_kind='FUTURE' AND scope_kind='CONTRACTS' "
                "AND effective_to IS NULL"), {"v": v}).scalar()
            if n:
                closable.append((v, n))

    print("\n== 待修复（DB=FREE 0，CSV 有值）==")
    for _rid, v, act, ft, fv, cft, cfv, cnote in plan:
        print("   %-6s %-12s %s%s -> %s %.4f  (CSV note: %s)"
              % (v, act, ft, fv, cft, cfv, cnote))
    print("== 待闭合的冗余分档 ==")
    for v, n in closable:
        print("   %-6s CONTRACTS %s 行（与 ALL 同值，用户确认无分档）" % (v, n))

    if not a.apply:
        print("\n[dry-run] --apply 才落库")
        return 0

    with session_scope() as s:
        for rid, v, act, _ft, _fv, cft, cfv, cnote in plan:
            s.execute(text(
                "UPDATE dim_trading_cost SET fee_type=:t, fee_value=:v, "
                "exchange_fee_value=:v, source='seed_csv_authoritative', "
                "note='修复: 原为FREE零成本(装载器未识别原文), 以种子CSV权威值重算 | ' "
                "     || COALESCE(:n,''), updated_at=now() WHERE id=:id"),
                {"t": cft, "v": cfv, "n": cnote, "id": rid})
        print("\n[done] 修复 %d 行零成本费率" % len(plan))
        for v, n in closable:
            s.execute(text(
                "UPDATE dim_trading_cost SET effective_to=CURRENT_DATE, updated_at=now(), "
                "note=COALESCE(note,'')||' | 025: 锡固定3元/手无分档(用户2026-10-04确认)' "
                "WHERE variety_code=:v AND instrument_kind='FUTURE' "
                "AND scope_kind='CONTRACTS' AND effective_to IS NULL"), {"v": v})
            print("[done] 闭合 %s 冗余 CONTRACTS %s 行" % (v, n))
        bad = s.execute(text(
            "SELECT count(*) FROM dim_trading_cost WHERE instrument_kind='FUTURE' "
            "AND scope_kind='ALL' AND action='OPEN' AND effective_to IS NULL "
            "AND (fee_type='FREE' OR fee_value=0)")).scalar()
        print("[verify] 修复后仍为零成本的期货 ALL/OPEN 行 = %s" % bad)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
