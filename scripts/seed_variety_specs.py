# -*- coding: utf-8 -*-
"""品种规格播种：把「代码侧字典」symbols.json 落到数据库字典表，消除多处真源。

背景
----
tick / multiplier / unit 这三项规格当前**同时存在三个真源**：
  1. ``app/ingest/fdf/symbols.json``（代码侧 JSON，交易所公开细则，50 品种，最权威）
  2. ``futures_symbol.price_tick`` / ``.multiplier``（库表；price_tick 由 seed_price_tick.py
     从行情最小正价差**派生**，multiplier 实测**全为 NULL**）
  3. ``dim_variety.tick_size`` / ``.multiplier``（字典表；007 回填把 tick_size 错位映射到
     fs.multiplier，故与 multiplier 同值且同样为空）

本脚本以 symbols.json 为**权威源**统一落库，并反向修复 dim_variety 的错位，
使「代码层不再持有规格真源」，符合字典/配置入库、代码与数据脱敏的要求。

交叉校验
--------
派生值（hourly_bar 相邻收盘最小正价差）与 symbols.json 权威值**逐品种比对**，
不一致只**告警不改写**（权威优先），派生值仅用于 symbols.json 未覆盖的品种兜底。

用法
----
  python scripts/seed_variety_specs.py            # dry-run：打印将写内容 + 交叉校验
  python scripts/seed_variety_specs.py --apply    # 落库
"""
from __future__ import annotations

import argparse
import json
import os

from sqlalchemy import text

from app.core.db import session_scope

SYMBOLS_JSON = os.path.join("app", "ingest", "fdf", "symbols.json")


def load_specs() -> dict[str, dict]:
    """读 symbols.json → {品种大写码: {tick, multiplier, unit, ...}}。"""
    with open(SYMBOLS_JSON, encoding="utf-8-sig") as f:
        raw = json.load(f)
    out: dict[str, dict] = {}
    for k, v in raw.items():
        if k.startswith("_"):
            continue
        out[k.split(".", 1)[1].upper()] = v
    return out


def derive_tick(session, symbol888: str) -> float | None:
    """从 888 连续序列派生 tick（相邻收盘最小正价差）；无数据返回 None。

    仅用于 symbols.json 未覆盖的品种兜底 —— 派生值是**下界近似**：
    若某品种行情从未出现单跳最小变动，派生值会偏大，故不作为权威。
    """
    rows = session.execute(text(
        "SELECT close FROM hourly_bar WHERE symbol=:s ORDER BY trade_datetime"),
        {"s": symbol888}).fetchall()
    prices = [float(r[0]) for r in rows if r[0] is not None]
    diffs = sorted({round(prices[i + 1] - prices[i], 6)
                    for i in range(len(prices) - 1) if prices[i + 1] - prices[i] > 0})
    return diffs[0] if diffs else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="落库；默认 dry-run")
    args = ap.parse_args()

    specs = load_specs()
    plan: list[dict] = []
    mismatches: list[str] = []

    with session_scope() as s:
        # futures_symbol 存的是 888 连续符号（MA888），品种码在 product 列
        syms = s.execute(text(
            "SELECT symbol, product, name, exchange, unit, multiplier, price_tick "
            "FROM futures_symbol ORDER BY symbol")).fetchall()
        print(f"[info] futures_symbol {len(syms)} 行；样例: "
              f"{[(r[0], r[1]) for r in syms[:4]]}")
        print("[info] dim_variety 样例: "
              f"{[(r[0], r[1]) for r in s.execute(text('SELECT variety_code, source FROM dim_variety ORDER BY variety_code LIMIT 6')).fetchall()]}")

        for sym, product, name, exch, unit, mult, ptick in syms:
            prod = (product or (sym[:-3] if sym.endswith("888") else sym)).upper()
            spec = specs.get(prod)
            derived = derive_tick(s, sym)
            if spec:
                tick = float(spec["tick"])
                new_mult = float(spec["multiplier"])
                new_unit = spec.get("unit") or unit
                src = "symbols.json"
            else:
                tick = derived
                new_mult = float(mult) if mult is not None else None
                new_unit = unit
                src = "derived" if tick else "none"
            if tick is None:
                continue
            # 交叉校验：派生 vs 权威
            if derived is not None and abs(float(derived) - tick) > 1e-9:
                mismatches.append(
                    f"  {prod}: 派生 tick={derived} ≠ 权威 tick={tick}（以权威为准，不改写）")
            plan.append({"symbol": sym, "tick": tick, "mult": new_mult,
                         "unit": new_unit, "src": src, "derived": derived,
                         "old_tick": ptick, "old_mult": mult})

        print(f"[计划] {len(plan)} 个品种；权威源 symbols.json 命中 "
              f"{sum(1 for p in plan if p['src'] == 'symbols.json')}，"
              f"派生兜底 {sum(1 for p in plan if p['src'] == 'derived')}")
        if mismatches:
            print(f"[交叉校验] 派生值与权威值不一致 {len(mismatches)} 处：")
            for m in mismatches:
                print(m)
        else:
            print("[交叉校验] 全部品种派生 tick 与 symbols.json 权威值一致 ✅")

        changed = [p for p in plan
                   if p["old_tick"] is None or abs(float(p["old_tick"]) - p["tick"]) > 1e-9
                   or (p["mult"] is not None and p["old_mult"] is None)]
        print(f"[变更] 需写入 {len(changed)} 行（tick 新增/修正 或 multiplier 由 NULL 补齐）")
        for p in changed[:12]:
            print(f"  {p['symbol']:<8} tick {p['old_tick']} → {p['tick']}  "
                  f"mult {p['old_mult']} → {p['mult']}  unit={p['unit']}  [{p['src']}]")
        if len(changed) > 12:
            print(f"  ... 另 {len(changed) - 12} 行")

        if not args.apply:
            print("[dry-run] --apply 才落库")
            return 0

        for p in plan:
            s.execute(text(
                "UPDATE futures_symbol SET price_tick=:t, "
                "multiplier=COALESCE(:m, multiplier), unit=COALESCE(:u, unit), "
                "updated_at=now() WHERE symbol=:s"),
                {"t": p["tick"], "m": p["mult"], "u": p["unit"], "s": p["symbol"]})
        print(f"[done] futures_symbol 已更新 {len(plan)} 行")

        # 反向修复 dim_variety：tick_size 应取 price_tick（007 曾错位取 multiplier）
        r1 = s.execute(text(
            "UPDATE dim_variety dv SET tick_size = fs.price_tick, updated_at=now() "
            "FROM futures_symbol fs WHERE fs.product = dv.variety_code "
            "AND fs.price_tick IS NOT NULL "
            "AND (dv.tick_size IS DISTINCT FROM fs.price_tick)"))
        print(f"[done] dim_variety.tick_size 修复 {r1.rowcount} 行（取 price_tick，非 multiplier）")
        r2 = s.execute(text(
            "UPDATE dim_variety dv SET multiplier = fs.multiplier, updated_at=now() "
            "FROM futures_symbol fs WHERE fs.product = dv.variety_code "
            "AND fs.multiplier IS NOT NULL "
            "AND (dv.multiplier IS DISTINCT FROM fs.multiplier)"))
        print(f"[done] dim_variety.multiplier 修复 {r2.rowcount} 行")
        r3 = s.execute(text(
            "UPDATE dim_variety dv SET quote_unit = fs.unit, updated_at=now() "
            "FROM futures_symbol fs WHERE fs.product = dv.variety_code "
            "AND fs.unit IS NOT NULL AND (dv.quote_unit IS DISTINCT FROM fs.unit)"))
        print(f"[done] dim_variety.quote_unit 修复 {r3.rowcount} 行")

        # 品种中文名：007 把中文名写在了 888 脏行上（已下线），正常行需从
        # futures_symbol.name 按 product 回填。中文名是「费率表中文名 → 品种码」
        # 映射的真源（见 scripts/export_variety_names.py），缺了它外部解析会落空。
        r3b = s.execute(text(
            "UPDATE dim_variety dv SET variety_name = fs.name, updated_at=now() "
            "FROM futures_symbol fs WHERE fs.product = dv.variety_code "
            "AND fs.name IS NOT NULL AND fs.name <> '' "
            "AND (dv.variety_name IS DISTINCT FROM fs.name)"))
        print(f"[done] dim_variety.variety_name 回填 {r3b.rowcount} 行")

        # 下线 007 误建的「888 连续码冒充品种码」脏行：variety_code 不该以 888 结尾
        r4 = s.execute(text(
            "UPDATE dim_variety SET is_active=false, "
            "source=COALESCE(source,'') || '+deprecated_888_symbol', updated_at=now() "
            "WHERE variety_code ~ '888$' AND is_active"))
        print(f"[done] dim_variety 下线 888 冒充品种码脏行 {r4.rowcount} 行（is_active=false，保留可审计）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
