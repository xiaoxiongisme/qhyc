# -*- coding: utf-8 -*-
"""cost.py 改动后的烟雾测试：确认费率仍可正常解析、且与加固前等价。

抽样策略池代表品种 + 昨天候选池品种，逐个走 OPEN/CLOSE_YEST/CLOSE_TODAY。
"""
from app.core.config import get_settings
from app.data.cost import fee_per_lot, fee_yuan_per_lot, fee_cache_stats

SEP = "=" * 78

settings = get_settings()
syms = [x.symbol for x in settings.main_contracts][:6]
extra = ["EB888", "CU888", "CF888", "SC888", "A888", "SM888", "AU888", "AG888", "LC888"]

print(SEP)
print("烟雾测试 —— fee_per_lot 解析是否正常")
print(SEP)
print(f"{'symbol':<9}{'OPEN':>12}{'CLOSE_YEST':>12}{'CLOSE_TODAY':>13}  {'scope':<10}备注")
print("-" * 78)

bad = 0
for s in dict.fromkeys(syms + extra):
    row = [s]
    srcs = []
    for act in ("OPEN", "CLOSE_YEST", "CLOSE_TODAY"):
        try:
            tc = fee_per_lot(s, act)
            val = tc.fee_yuan
            row.append(f"{val:,.4f}")
            srcs.append(tc.scope_kind)
        except Exception as e:
            row.append("ERR")
            srcs.append("ERR")
            bad += 1
            print("   ", s, act, str(e)[:100])
    print(f"{row[0]:<9}{row[1]:>12}{row[2]:>12}{row[3]:>13}  {srcs[0]:<10}"
          f"{'含 fallback' if any('F' in x for x in srcs) else ''}")

print("-" * 78)
print(f"异常数: {bad}")

print()
print(SEP)
print("缓存统计（same_rank_multi 应恒为 0）")
print(SEP)
for k, v in fee_cache_stats().items():
    print(f"    {k:<22}{v}")

print()
print(SEP)
print("端到端：折算成元/手")
print(SEP)
for s in ("RB888", "CU888", "SC888", "AU888"):
    try:
        for act in ("OPEN", "CLOSE_YEST"):
            y = fee_yuan_per_lot(s, act, price=50000.0)
            print(f"    {s:<8}{act:<12}@50000 -> {y:,.2f} 元/手")
    except Exception as e:
        print(f"    {s} ERR {str(e)[:90]}")

print(SEP)
