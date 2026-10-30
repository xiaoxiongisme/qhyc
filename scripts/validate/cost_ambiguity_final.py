# -*- coding: utf-8 -*-
"""费率歧义的终局验证 —— 覆盖 cost.py 的全部两条查询路径与全部 12 个合约月。

路径A（历史有效区间查询）：effective_from <= d AND (to IS NULL OR to > d)
路径B（fallback 查询）    ：effective_to IS NULL   ← 历史回测必然走这条
"""
from collections import defaultdict

from sqlalchemy import text
from app.core.db import session_scope

SCOPE_RANK = {"CONTRACTS": 3, "MONTHS": 2, "ALL": 1}
SEP = "=" * 78
ALL_MONTHS = list(range(1, 13))

SQL_A = """
    SELECT id, variety_code, action, scope_kind, scope_months, scope_contracts,
           fee_type, fee_value, exchange_fee_value,
           broker_markup_type, broker_markup_value, effective_from, source
    FROM dim_trading_cost
    WHERE instrument_kind='FUTURE'
      AND effective_from <= :d AND (effective_to IS NULL OR effective_to > :d)
"""
SQL_B = """
    SELECT id, variety_code, action, scope_kind, scope_months, scope_contracts,
           fee_type, fee_value, exchange_fee_value,
           broker_markup_type, broker_markup_value, effective_from, source
    FROM dim_trading_cost
    WHERE instrument_kind='FUTURE' AND effective_to IS NULL
"""


def hit(r, cmonth):
    sk = r[3]
    if sk not in ("CONTRACTS", "MONTHS"):
        return True
    ms = []
    for x in (r[4] or []):
        try:
            ms.append(int(x))
        except (TypeError, ValueError):
            pass
    return cmonth in ms


def sig(r):
    return (r[6], round(float(r[7] or 0), 10), round(float(r[8] or 0), 10),
            r[9], round(float(r[10] or 0), 10))


def scan(rows, label):
    print()
    print(SEP)
    print(f"[{label}]  生效行数={len(rows)}")
    print(SEP)
    g = defaultdict(list)
    for r in rows:
        g[(r[1], r[2])].append(r)
    found = {}
    for (vc, act), rs in sorted(g.items()):
        for m in ALL_MONTHS:
            hits = [r for r in rs if hit(r, m)]
            if len(hits) < 2:
                continue
            top_rank = max(SCOPE_RANK.get(r[3], 0) for r in hits)
            top = [r for r in hits if SCOPE_RANK.get(r[3], 0) == top_rank]
            if len(top) < 2:
                continue
            if len({sig(r) for r in top}) < 2:
                continue
            key = (vc, act, m)
            found.setdefault(key, []).extend(top)
    if not found:
        print("  真·歧义: 0  ✅")
    else:
        print(f"  真·歧义组合: {len(found)}")
        seen = set()
        for (vc, act, m), top in sorted(found.items()):
            ids = tuple(r[0] for r in top)
            if ids in seen:
                continue
            seen.add(ids)
            print(f"\n  [{vc}/{act}] 月={m}")
            for r in dict.fromkeys(top, lambda: None) if False else top:
                print(f"     id={r[0]:<6} scope={r[3]:<10} {r[6]:<6} "
                      f"fee={float(r[7] or 0):<9.4f} ex={float(r[8] or 0):<9.4f} "
                      f"from={r[11]} src={r[12]}")
    return len(found)


with session_scope() as s:
    today = s.execute(text("SELECT current_date")).scalar()
    rows_a = s.execute(text(SQL_A), {"d": today}).fetchall()
    rows_b = s.execute(text(SQL_B)).fetchall()

n_a = scan(rows_a, f"路径A 当日, d={today}")
n_b = scan(rows_b, "路径B fallback (effective_to IS NULL)")

print()
print(SEP)
print("同一 to IS NULL 下、按 (品种,action) 统计的多余存活记录")
print(SEP)
g = defaultdict(list)
for r in rows_b:
    g[(r[1], r[2])].append(r)
multi = {k: v for k, v in g.items() if len(v) > 1}
print(f"  有 >1 条 (品种,action): {len(multi)} 组")
print("  其中按 scope_kind 区分后仍同档多条的才是风险。逐组看:")
risk = 0
for (vc, act), rs in sorted(multi.items()):
    by_scope = defaultdict(list)
    for r in rs:
        by_scope[r[3]].append(r)
    for sk, lst in by_scope.items():
        if len(lst) > 1:
            sigs = {sig(r) for r in lst}
            flag = "值不同★" if len(sigs) > 1 else "值相同"
            print(f"    {vc:<6}{act:<12} scope={sk:<10} n={len(lst)} {flag} "
                  f"ids={[r[0] for r in lst]}")
            if len(sigs) > 1:
                risk += 1
print(f"\n  同 scope 且值不同的组数: {risk}")
print(SEP)
print(f"\n结论: 路径A真歧义={n_a}, 路径B真歧义={n_b}, 同档多值组={risk}")
print(SEP)
