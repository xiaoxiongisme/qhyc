# -*- coding: utf-8 -*-
"""Lint：扫描散落在「数据层网关之外」的直接行情表裸引用。

背景（用户 2026-09-30 要求：只读品种代码+合约名，去掉中间主连/各合约转换）
-------------------------------------------------------------------
已固化网关为 `app/data/barstore.py`（load/resolve_symbol）与
`app/data/caliber.py`（ROUTES 注册表）。任何「不经网关裸写 SQL 读行情表」
的写法都会在跨周期 join 时静默少数据（现状梳理_01 已点名）。

本脚本列出网关之外的裸引用，供整改清单使用（不自动改写，避免无本地DB验证的风险）。
用法
----
    python scripts/lint_direct_table_refs.py            # 扫描并报告
    python scripts/lint_direct_table_refs.py --fix-dry  # 仅打印建议（与默认同）
退出码 0（纯报告工具）；发现引用时打印到 stderr 并统计。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 受管行情/复权表（必须经 barstore.load / caliber 路由）
GUARDED_TABLES = [
    r"fut_kline", r"minute_bar", r"minute_bar_adj", r"minute_bar_stage",
    r"bar_5m", r"bar_15m", r"bar_30m", r"bar_60m",
    r"daily_bar", r"hourly_bar", r"main_continuous",
    r"roll_segment", r"main_contract_map", r"contract_code_map",
]

# 不在网关内、不应裸引用的目录（网关本身 + 允许裸用的基础设施）
EXCLUDE_DIRS = {
    "app/data",          # 网关本身（barstore/caliber/back_adjust）
    "migrations",        # DDL 天然要写表名
    "db",                # 初始化 SQL / 引擎
    "tests", "vendor", "web", "imports", "qhyc_review_tmp",
}
# 文件名前缀为 _ 的临时诊断脚本（如 _diag_*.py）也排除
EXCLUDE_FILE_PREFIX = ("_",)

TABLE_RE = re.compile(
    r"\b(" + "|".join(GUARDED_TABLES) + r")\b", re.IGNORECASE)


def iter_py_files():
    for p in (ROOT / "app").rglob("*.py"):
        rel = p.relative_to(ROOT)
        parts = set(rel.parts[:-1])
        if parts & EXCLUDE_DIRS:
            continue
        if rel.name.startswith(EXCLUDE_FILE_PREFIX):
            continue
        yield p
    # scripts/ 下非下划线前缀的也扫描（采集/派生脚本常裸读行情表）
    for p in (ROOT / "scripts").rglob("*.py"):
        rel = p.relative_to(ROOT)
        if rel.name.startswith(EXCLUDE_FILE_PREFIX):
            continue
        yield p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix-dry", action="store_true",
                    help="仅打印建议（默认即如此，占位兼容）")
    args = ap.parse_args()

    hits: dict[Path, list[tuple[int, str, str]]] = {}
    for p in iter_py_files():
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for i, ln in enumerate(lines, 1):
            m = TABLE_RE.search(ln)
            if m:
                tbl = m.group(1).lower()
                hits.setdefault(p, []).append((i, tbl, ln.strip()))

    total = sum(len(v) for v in hits.values())
    print(f"== 行情表裸引用扫描（网关外）==")
    print(f"扫描根: {ROOT}")
    print(f"受管表: {', '.join(sorted(set(GUARDED_TABLES)))}")
    print(f"排除目录: {', '.join(sorted(EXCLUDE_DIRS))}")
    print(f"发现引用: {total} 处 / {len(hits)} 个文件\n")

    if not hits:
        print("✅ 未发现网关外裸引用。")
        return 0

    for p in sorted(hits):
        print(f"📄 {p.relative_to(ROOT)}  ({len(hits[p])} 处)")
        for i, tbl, ln in hits[p]:
            print(f"   L{i:<5} [{tbl}]  {ln[:120]}")
        print()

    print("整改建议：上述裸引用统一改为 "
          "`from app.data import barstore; barstore.load(symbol, freq, caliber, ...)`，"
          "symbol 只传品种代码（如 'FG'）或合约码，888/8888/KQ.m@ 转换由 dim_symbol 完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
