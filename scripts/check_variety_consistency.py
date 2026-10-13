#!/usr/bin/env python
"""品种规格「代码内来源 vs dim_variety」一致性核对 —— 补齐缺失的报警机制。

背景：``config/*.yaml`` 的 ``ingest.main_contracts``、``blocked_varieties._BLOCKED``、
``inventory.PRODUCT_TO_EM_SYM`` 与 ``dim_variety`` 是**多份品种元数据**。历史上代码内
字典与库表不一致时**没有任何机制报警**（新品种/改乘数只改一处 → 其余静默走错分支，
见 tasklog 中「开关看似生效、实则静默取不到数据」同族教训）。

本脚本把上述代码内来源逐条与 ``dim_variety`` 比对并打印分歧清单：
- config.main_contracts：品种缺失 / 乘数不一致 / 交易所不一致 / 名称不一致
- blocked_varieties._BLOCKED：品种缺失 / 与 dim_variety.is_active 的关系
- inventory.PRODUCT_TO_EM_SYM：品种缺失

用法::

    python scripts/check_variety_consistency.py            # 全量核对，只报告
    python scripts/check_variety_consistency.py --strict   # 有分歧即 exit 1（可接入 CI）

退出码：0=无分歧；1=有分歧(仅 --strict)；2=连不上库或表缺失。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.logging import setup_logging
from app.ingest.blocked_varieties import BLOCKED_VARIETY_CODES
from app.ingest.inventory import PRODUCT_TO_EM_SYM


def load_dim_variety() -> dict:
    """dim_variety 全量 → {UPPER(variety_code): {exchange, multiplier, name, is_active}}。"""
    out: dict = {}
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT upper(variety_code), exchange, multiplier, variety_name, is_active "
            "FROM dim_variety"
        )).fetchall()
    for code, exchange, mult, name, is_active in rows:
        out[code] = {"exchange": exchange, "multiplier": mult,
                     "name": name, "is_active": is_active}
    return out


def find_divergences(db: dict) -> list[str]:
    divergences: list[str] = []

    # 1) config.main_contracts vs dim_variety
    for mc in get_settings().main_contracts:
        code = (mc.product or "").upper()
        row = db.get(code)
        if row is None:
            divergences.append(f"[config] {mc.symbol}({code}) 不在 dim_variety")
            continue
        try:
            same_mult = float(mc.multiplier) == float(row["multiplier"] or 0)
        except (TypeError, ValueError):
            same_mult = False
        if not same_mult:
            divergences.append(
                f"[config] {code} 乘数不一致: yaml={mc.multiplier} "
                f"dim_variety={row['multiplier']}")
        if (mc.exchange or "").upper() != (row["exchange"] or "").upper():
            divergences.append(
                f"[config] {code} 交易所不一致: yaml={mc.exchange} "
                f"dim_variety={row['exchange']}")
        if mc.name and row["name"] and mc.name != row["name"]:
            divergences.append(
                f"[config] {code} 名称不一致: yaml={mc.name} dim_variety={row['name']}")

    # 2) blocked_varieties vs dim_variety.is_active
    for code in sorted(BLOCKED_VARIETY_CODES):
        row = db.get(code)
        if row is None:
            divergences.append(
                f"[blocked] {code} 不在 dim_variety（封禁清单引用了库里没有的品种）")
        elif row["is_active"]:
            divergences.append(
                f"[blocked] {code} 在封禁清单但 dim_variety.is_active=true"
                f"（夜间全量重建可能把它重新插回）")

    # 3) inventory em 代号 vs dim_variety（存在性；厂商能力映射本就可缺，仅提示缺失）
    for product in sorted(PRODUCT_TO_EM_SYM):
        if product.upper() not in db:
            divergences.append(f"[inventory] em 代号 {product} 不在 dim_variety")

    return divergences


def main() -> int:
    ap = argparse.ArgumentParser(description="品种规格 代码内来源 vs dim_variety 一致性核对")
    ap.add_argument("--strict", action="store_true", help="有分歧即 exit 1")
    args = ap.parse_args()

    setup_logging()
    try:
        db = load_dim_variety()
    except Exception as e:  # noqa: BLE001
        print(f"[consistency] 无法读取 dim_variety（库不可达或表未建）: {e}")
        print("[consistency] 请先跑 migrations/007_dim_tables.sql + "
              "scripts/seed_variety_specs.py --apply")
        return 2
    if not db:
        print("[consistency] dim_variety 为空 —— 先播种品种字典")
        return 2

    divergences = find_divergences(db)
    print(f"[consistency] dim_variety={len(db)} 品种；"
          f"config.main_contracts={len(get_settings().main_contracts)}；"
          f"blocked={len(BLOCKED_VARIETY_CODES)}；"
          f"inventory_em={len(PRODUCT_TO_EM_SYM)}")
    if not divergences:
        print("[consistency] ✅ 代码内来源与 dim_variety 一致，无分歧")
        return 0
    print(f"[consistency] ⚠️ 发现 {len(divergences)} 处分歧：")
    for d in divergences:
        print("   -", d)
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
