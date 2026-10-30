# -*- coding: utf-8 -*-
"""验证 2026-10-31 对 fee_per_lot 的 ORDER BY 加固是否改变任何费率取值。

背景：加固声称「当前库中同 rank 不同值的组合数为 0，故取值完全不变」。
本脚本用静态穷举证明这一点，而不是靠断言。

旧行为（未加 ORDER BY）：PG 按物理堆顺序返回 —— 实践中堆顺序基本等价于
  插入顺序，即 id 升序；故「 rank 相同保留先遍历到的那条」≈ 取 id 最小者。
新行为（ORDER BY effective_from DESC, id DESC）：取 effective_from 最新者，
  再按 id 降序。

比较口径：对每个 (品种, action, 合约月) 组合，先筛出「最高 rank 且命中该月」
的候选集，然后：
  OLD = 候选中 id 最小者
  NEW = 候选中 (effective_from DESC, id DESC) 排序的首个
若两者费率签名一致 → 加固对该组合无影响。
覆盖 FUTURE 全部品种 × OPEN/CLOSE_YEST/CLOSE_TODAY × 1~12 月。
"""
from collections import defaultdict

from sqlalchemy import text
from app.core.db import session_scope
from app.core.config import get_settings

SCOPE_RANK = {"CONTRACTS": 3, "MONTHS": 2, "ALL": 1}
SEP = "=" * 78


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
    """最终会被 TradingCost 采用的字段。"""
    return (r[6], round(float(r[7] or 0), 10), round(float(r[8] or 0), 10),
            r[9], round(float(r[10] or 0), 10), round(float(r[12] or 0), 10))


with session_scope() as s:
    rows = s.execute(text("""
        SELECT id, variety_code, action, scope_kind, scope_months, scope_contracts,
               fee_type, fee_value, exchange_fee_value, broker_markup_type,
               broker_markup_value, effective_from, slip_ticks, source
        FROM dim_trading_cost
        WHERE instrument_kind='FUTURE' AND effective_to IS NULL
    """)).fetchall()

print(SEP)
print(f"等价性验证 —— 候选行数={len(rows)}")
print(SEP)

g = defaultdict(list)
for r in rows:
    g[(r[1], r[2])].append(r)

diffs = []
checked = 0
multi_rank = 0
for (vc, act), rs in sorted(g.items()):
    for m in range(1, 13):
        hits = [r for r in rs if hit(r, m)]
        if not hits:
            continue
        checked += 1
        top_rank = max(SCOPE_RANK.get(r[3], 0) for r in hits)
        top = [r for r in hits if SCOPE_RANK.get(r[3], 0) == top_rank]
        if len(top) > 1:
            multi_rank += 1
        old = min(top, key=lambda x: x[0])                       # id 升序首条
        new = sorted(top, key=lambda x: (x[11], x[0]),
                     reverse=True)[0]                            # from DESC, id DESC
        if sig(old) != sig(new):
            diffs.append((vc, act, m, old, new))

print(f"  校验组合数 (品种,action,月): {checked}")
print(f"  其中最高 rank 有多条候选的: {multi_rank}")
print()
if diffs:
    print(f"  ❌ 存在差异的组合数: {len(diffs)}")
    print(SEP)
    for vc, act, m, o, n in diffs[:20]:
        print(f"  [{vc}/{act}] 月={m}")
        print(f"     OLD id={o[0]} {o[6]} fee={float(o[7] or 0)} ex={float(o[8] or 0)} "
              f"from={o[11]} src={o[13]}")
        print(f"     NEW id={n[0]} {n[6]} fee={float(n[7] or 0)} ex={float(n[8] or 0)} "
              f"from={n[11]} src={n[13]}")
else:
    print("  ✅ 全部组合等价 —— ORDER BY 加固不改变任何费率取值")
print(SEP)

# ── 补充：证明「为什么」等价 —— 按 (品种,action,scope_kind) 统计候选条数 ──
print()
print(SEP)
print("等价的根因：按 (品种, action, scope_kind) 分组后，是否真的只有一条")
print(SEP)
cnt = defaultdict(list)
for r in rows:
    cnt[(r[1], r[2], r[3])].append(r)
multi = {k: v for k, v in cnt.items() if len(v) > 1}
if not multi:
    print("  ✅ 所有 (品种,action,scope_kind) 均只有 1 条 → 排序不可能影响结果")
else:
    print(f"  ⚠ 存在多条的组: {len(multi)}")
    for k, v in multi.items():
        print(f"    {k[0]:<6}{k[1]:<12}{k[2]:<10} n={len(v)} ids={[x[0] for x in v]}")
print(SEP)
