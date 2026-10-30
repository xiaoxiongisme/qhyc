# -*- coding: utf-8 -*-
"""费率表自身质量检查。"""
import csv
import io
from collections import defaultdict

P = "E:/抖音分析/2026-10-29-21-14-18/qhyc完整费率表_dim_trading_cost.csv"
rows = list(csv.DictReader(io.open(P, encoding="utf-8-sig")))
print("总行数:", len(rows))
print("列:", list(rows[0].keys()))

TODAY = "2026-10-31"

def f(v):
    try:
        return float(v)
    except Exception:
        return None

def in_effect(r):
    fr = r["effective_from"]
    to = r["effective_to"]
    if not fr or fr > TODAY:
        return False
    if to and to < TODAY:
        return False
    return True

eff = [r for r in rows if in_effect(r)]
print("当前生效:", len(eff), " 品种:", len({r["variety_code"] for r in eff}))

# 1) action 完整性
print("\n" + "=" * 80)
print("检查1：每个生效品种是否都具备 OPEN / CLOSE_YEST")
print("=" * 80)
byv = defaultdict(set)
for r in eff:
    byv[r["variety_code"]].add(r["action"])
bad = []
for v, acts in sorted(byv.items()):
    missing = [a for a in ("OPEN", "CLOSE_YEST") if a not in acts]
    if missing:
        bad.append((v, missing))
print("缺 OPEN/CLOSE_YEST 的品种数:", len(bad))
for v, m in bad:
    print("  ", v, "缺", m)

# 2) broker_markup 一致性（同一品种不同 action 应一致）
print("\n" + "=" * 80)
print("检查2：同一品种 broker_markup_value 是否一致")
print("=" * 80)
bm = defaultdict(set)
for r in eff:
    bm[r["variety_code"]].add(r["broker_markup_value"])
incons = [(v, vals) for v, vals in sorted(bm.items()) if len(vals) > 1]
print("不一致品种数:", len(incons))
for v, vals in incons:
    print("  ", v, vals)

# 3) broker_markup = 0 的（可疑，正常应为 0.01）
print("\n" + "=" * 80)
print("检查3：broker_markup_value = 0（券商不加价，可疑）")
print("=" * 80)
zero = [(r["variety_code"], r["action"], r["source"]) for r in eff if f(r["broker_markup_value"]) == 0.0]
print("条目数:", len(zero), " 品种:", sorted({z[0] for z in zero}))
for z in zero[:20]:
    print("  ", z)

# 4) fee_value = 0 且 fee_type != FREE
print("\n" + "=" * 80)
print("检查4：fee_value = 0 但 fee_type 不是 FREE")
print("=" * 80)
z = [(r["variety_code"], r["action"], r["fee_type"], r["fee_value"]) for r in eff
     if f(r["fee_value"]) == 0.0 and r["fee_type"] != "FREE"]
print("条目数:", len(z))
for x in z[:30]:
    print("  ", x)

# 5) slip_ticks 是否都是 1
print("\n" + "=" * 80)
print("检查5：slip_ticks 分布")
print("=" * 80)
from collections import Counter
print(dict(Counter(r["slip_ticks"] for r in eff)))

# 6) PCT 类型的 fee_value 量级（应是万分比或百分比）
print("\n" + "=" * 80)
print("检查6：PCT 类型 fee_value 量级")
print("=" * 80)
pct = [r for r in eff if r["fee_type"] == "PCT"]
print("PCT 条目:", len(pct))
vals = sorted({round(f(r["fee_value"]), 8) for r in pct if f(r["fee_value"]) is not None})
print("不同取值:", vals[:20])
show_map = defaultdict(set)
for r in pct:
    show_map[r["variety_code"]].add(r["fee_value"])
for v in sorted(show_map):
    print(f"  {v:<6} {sorted(show_map[v])}")

# 7) 一个品种多个生效条目（同 action 重复）
print("\n" + "=" * 80)
print("检查7：同一 (品种, action) 是否有多个同时生效条目")
print("=" * 80)
key = defaultdict(list)
for r in eff:
    key[(r["variety_code"], r["action"])].append(r["id"])
dup = [(k, v) for k, v in sorted(key.items()) if len(v) > 1]
print("冲突数:", len(dup))
for k, v in dup[:20]:
    print("  ", k, v)
