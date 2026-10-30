# -*- coding: utf-8 -*-
"""导出完整费率表 dim_trading_cost -> CSV / JSON。"""
import csv
import json
from decimal import Decimal
from sqlalchemy import text
from app.core.db import session_scope

COLS = [
    "id", "exchange", "variety_code", "instrument_kind", "action",
    "scope_kind", "scope_months", "scope_contracts",
    "fee_type", "fee_value", "exchange_fee_value",
    "broker_markup_type", "broker_markup_value", "slip_ticks",
    "effective_from", "effective_to", "source", "note",
    "updated_at", "overwritten_from",
]

with session_scope() as s:
    rows = s.execute(text("SELECT %s FROM l3_ref.dim_trading_cost ORDER BY variety_code, action, effective_from" % ", ".join(COLS))).fetchall()


def norm(v):
    if isinstance(v, Decimal):
        return float(v)
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "|".join(str(x) for x in v)
    return str(v)


records = []
for r in rows:
    m = r._mapping
    records.append({c: norm(m[c]) for c in COLS})

out_json = "/tmp/cost_full.json"
out_csv = "/tmp/cost_full.csv"

with open(out_json, "w", encoding="utf-8") as f:
    json.dump(records, f, ensure_ascii=False, indent=1)

with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=COLS)
    w.writeheader()
    for rec in records:
        w.writerow(rec)

print("导出行数:", len(records))
print("JSON:", out_json)
print("CSV :", out_csv)

# 摘要
from collections import Counter, defaultdict
print("\n按品种数:", len({r["variety_code"] for r in records}))
print("按交易所:", dict(Counter(r["exchange"] for r in records)))
print("按 action:", dict(Counter(r["action"] for r in records)))
print("按 fee_type:", dict(Counter(r["fee_type"] for r in records)))
print("source:", dict(Counter(r["source"] for r in records)))

# 有效(in effect)条目 vs 历史条目
today = "2026-10-31"
eff = [r for r in records if r["effective_from"] <= today and (not r["effective_to"] or r["effective_to"] >= today)]
print("\n当前生效条目:", len(eff), " / 生效品种:", len({r["variety_code"] for r in eff}))

# 一个品种展示一行: OPEN + CLOSE_YEST
print("\n" + "=" * 92)
print("当前生效费率一览（每品种 OPEN / CLOSE_YEST / CLOSE_TODAY）")
print("=" * 92)
bys = defaultdict(dict)
for r in eff:
    bys[r["variety_code"]][r["action"]] = r
print(f"{'品种':<6}{'交易所':<8}{'类型':<8}{'手续费值':>12}{'交易所收':>12}{'券商加价':>10}{'滑点(跳)':>9}")
print("-" * 92)
for v in sorted(bys):
    # 取所有 action
    for act in sorted(bys[v]):
        r = bys[v][act]
        print(f"{v if act == sorted(bys[v])[0] else '':<6}{r['exchange']:<8}{act:<12}"
              f"{r['fee_value']!s:>12}{r['exchange_fee_value']!s:>12}"
              f"{r['broker_markup_value']!s:>10}{r['slip_ticks']!s:>9}")
