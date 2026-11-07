# -*- coding: utf-8 -*-
"""验证成本 resolver：真实费率、合约范围、三动作、滑点（临时，只读）。"""
from datetime import date
from app.data.cost import fee_per_lot, fee_yuan_per_lot, round_trip_cost, CostNotFoundError
from app.data.barstore import variety_spec

D = date(2026, 10, 3)
print("=== 1. 按合约范围取费率（螺纹钢：全范围 0.2permille，特定合约 1permille）===")
for c, label in [(None, "RB2610 (全范围)"), ("RB2602", "RB2602 (特定)"), ("RB2603", "RB2603 (特定)"), ("RB2611", "RB2611 (全范围)")]:
    tc = fee_per_lot("RB888", "OPEN", contract=c, on_date=D)
    print(f"  {label:<18} scope={tc.scope_kind:<10} {tc.fee_type} {tc.exchange_fee_yuan}permille broker+{tc.broker_markup_yuan} slip={tc.slip_ticks}")

print("\n=== 2. 三动作（苹果 AP：开仓5 / 平昨5 / 平今20 固定值）===")
for a in ("OPEN", "CLOSE_YEST", "CLOSE_TODAY"):
    tc = fee_per_lot("AP888", a, on_date=D)
    print(f"  {a:<12} {tc.fee_type:<6} {tc.exchange_fee_yuan} 元/手  scope={tc.scope_kind}  note={tc.note}")

print("\n=== 3. 百分比折算成元/手（螺纹 3100 x 10 x 0.2permille = 6.2 + 0.01 券商）===")
y = fee_yuan_per_lot("RB888", "OPEN", 3100.0, on_date=D)
print(f"  RB3100 开仓 = {y:.4f} 元/手  (交易所6.2 + 券商0.01)")

print("\n=== 4. 一次开平总成本（含滑点 1跳/边 => 2跳）===")
for sym, px in (("RB888", 3100.0), ("AP888", 8000.0), ("CU888", 78000.0), ("AU888", 900.0)):
    r = round_trip_cost(sym, px, on_date=D, lots=1)
    print(f"  {sym:<7} price={px:<9} open={r['fee_open']:.2f} close={r['fee_close']:.2f} "
          f"slip={r['slippage']:.2f} total={r['total']:.2f}  (close_action={r['close_action']}, tick={r['tick_size']}, mult={r['multiplier']})")

print("\n=== 5. 未知即报错（不得按 0 成本）===")
for sym, act in (("ZZ888", "OPEN"), ("RB888", "CLOSE_TODAY")):
    try:
        tc = fee_per_lot(sym, act, on_date=D)
        print(f"  {sym}/{act} -> {tc.fee_type} {tc.exchange_fee_yuan}")
    except CostNotFoundError as e:
        print(f"  {sym}/{act} -> OK 按预期报错: {str(e)[:66]}")

print("\n=== 6. 铜：全范围 0.5permille ===")
tc = fee_per_lot("CU888", "OPEN", on_date=D)
print(f"  CU OPEN {tc.fee_type} {tc.exchange_fee_yuan}permille -> {fee_yuan_per_lot('CU888','OPEN',78000.0,on_date=D):.2f} 元/手")
