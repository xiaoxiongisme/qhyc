#!/usr/bin/env python
"""把实测结论写回 factor_registry（幂等，可重复执行）。

**为什么需要（2026-10-07）**：WB《海龟×融合叠加回测报告》实测发现
``f_atr_pctile`` 登记表写的权重建议「ATR 分位 >90% 降仓」**对本系统信号反向**
（海龟 all 口径净 156万→44万；融合 IS 直接转负 13.9万→−21.7万）。
但登记表里那条建议仍在原地，后人照表上生产会重蹈覆辙 —— 这正是
memory 里「登记表里的结论必须与实测量化绑定」要求的落地。

同时把各因子的**真实历史覆盖**写进描述。报告 §3.1 的覆盖分级基于旧库、
已过时（实测：会员持仓 z 与基差 z 均为 2015 起全覆盖，而非 2023/数天），
登记表若不写明覆盖，后人会误判「因子不可用」而白丢数据。

用法::

    python scripts/annotate_factor_registry.py --dry-run   # 只看会改什么
    python scripts/annotate_factor_registry.py             # 落库
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault(
    "DATABASE_URL", "postgresql://futures:qhyc_dev_pwd_2026@127.0.0.1:15432/futures")

from sqlalchemy import text  # noqa: E402

from app.core.db import get_engine  # noqa: E402

#: 因子 → 追加到 description 末尾的实测标注。
#: 全部为 2026-10-07 云端库实测（表 factor_value 逐因子 min/max 复核）。
ANNOTATIONS: dict[str, str] = {
    "f_atr_pctile": (
        "【2026-10-07 实测反向，勿照原建议上生产】原建议「ATR 分位 >90% 降仓、"
        "<10% 警戒」对本系统信号**反向**：①海龟全品种口径净 156.4万→44万；"
        "②海龟 sel 口径微升 156→163万但全品种崩至 44万；③融合 IS 直接转负 "
        "13.9万→−21.7万。原因：趋势发动段恰处高 ATR 分位，门控砍掉的正是启动行情。"
        "结论：趋势策略右尾不可剪，任何「砍右尾」的权重建议一律先走同款 walk-forward 再采纳。"
    ),
    "warehouse_receipt_z": (
        "【覆盖实测 2026-10-07】factor_value 实覆盖 2020-07-02 起、71 品种。"
        "**IS 段（2015-2020）无数据是源表限制**（warehouse_receipt 最早 2020-07-02），"
        "非计算缺失；故本因子无法满足真正 IS→OOS 纪律，引用时必须声明。"
    ),
    "inventory_z": (
        "【覆盖实测 2026-10-07】factor_value 实覆盖 2026-06-01 起、44 品种（3,193 行），"
        "与上方 2026-09-29 退役判断一致。恢复采集后需重新评估退役结论。"
    ),
    "basis_rate_z": (
        "【覆盖实测 2026-10-07】factor_value 实覆盖 **2015-01-05 起、56 品种、12.5 万行**。"
        "旧登记曾记「仅数天覆盖」——**该结论已过时**（基于旧库/未重算）。"
        "本因子现已满足完整 IS→OOS 条件，可纳入 walk-forward。"
    ),
    "member_net_z": (
        "【覆盖实测 2026-10-07】factor_value 实覆盖 **2015-01-05 起、82 品种、9.4 万行**"
        "（源表 member_position_rank 2015-01-05 起 834 万行）。"
        "旧登记曾记「自 2023 起」——**该结论已过时**。本因子可纳入完整 IS→OOS。"
    ),
    "member_ls_z": (
        "【覆盖实测 2026-10-07】factor_value 实覆盖 **2015-01-05 起、82 品种**。"
        "旧登记曾记「自 2023 起」——**该结论已过时**。本因子可纳入完整 IS→OOS。"
    ),
    "member_trend_z": (
        "【覆盖实测 2026-10-07】factor_value 实覆盖 **2015-01-06 起、74 品种**。"
        "旧登记曾记「自 2023 起」——**该结论已过时**。本因子可纳入完整 IS→OOS。"
    ),
    "roll_yield_z": (
        "【覆盖实测 2026-10-07】factor_value 实覆盖 2018-01-02 起、33 品种。"
        "注：roll_yield 的 DCE 品种自 2026-10-06 起由库内 contract_daily 兜底补齐"
        "（akshare get_roll_yield 对 DCE 返回非 JSON），覆盖率后续会继续提升。"
    ),
}

#: 标注哨兵——已含该标记则跳过，保证幂等。
SENTINEL = "【覆盖实测 2026-10-07】"
REVERSED_SENTINEL = "【2026-10-07 实测反向，勿照原建议上生产】"


def main() -> int:
    ap = argparse.ArgumentParser(description="把实测结论写回 factor_registry（幂等）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将发生的变更")
    args = ap.parse_args()

    eng = get_engine()
    changed = 0
    with eng.connect() as conn:
        for fid, note in ANNOTATIONS.items():
            row = conn.execute(text(
                "select description from factor_registry where factor_id=:f"),
                {"f": fid}).fetchone()
            if row is None:
                print(f"  [跳过] {fid} 不在 factor_registry")
                continue
            desc = row[0] or ""
            marks = [m for m in (SENTINEL, REVERSED_SENTINEL) if m in desc]
            if marks:
                print(f"  [已标注] {fid}（含 {marks[0]}…），跳过")
                continue
            new = (desc.rstrip() + " " + note).strip()
            print(f"  [更新] {fid}: description {len(desc)} → {len(new)} 字")
            print(f"          追加: {note[:90]}…")
            if not args.dry_run:
                conn.execute(text(
                    "update factor_registry set description=:d where factor_id=:f"),
                    {"d": new, "f": fid})
            changed += 1
        if not args.dry_run:
            conn.commit()
    print(f"\n{'待' if args.dry_run else '已'}更新 {changed} 条"
          f"{'（dry-run，未落库）' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
