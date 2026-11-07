#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""死代码巡检：**已实现但无任何调用方**的公开能力（2026-11-06 新增）。

为什么需要它
------------
本项目最主要的缺陷类型是「开关看似生效、实则静默取不到数据/未启用」。
死代码是它的另一面：**能力已实现、却没有任何调用方** —— 于是
"这个功能到底有没有接进生产"只能靠人肉翻代码。历史上有 4 项能力型死代码
（``load_brake_config`` / ``cross_section_ic`` / ``find_order_by_key`` /
``basis_factors``）就是这样长期空转的。

本脚本把这件事**变成可巡检的**：
  * AST 扫出 ``app/`` 下所有**模块级**公开 def/class；
  * 排除 **FastAPI 路由端点**（装饰器里有 ``router.`` —— 它们由框架注册，
    文本扫描看不见，但**不是**死代码）；
  * 排除被 ``from ... import`` re-export 的名字（``__init__.py`` 聚合导出）；
  * 全仓（含 tests/）统计该名字的**文本出现次数**；
  * 出现次数 ≤ 1（即只有定义处）→ 判定为「已实现未接线」。

⚠ 判据是**文本引用计数**（近似 LSP findReferences）：同名符号在不同模块里
  偶然同名会造成假阴性（误判为已接线）。宁可漏报也不误报。

用法::

    python scripts/check_dead_code.py            # 报告
    python scripts/check_dead_code.py --strict   # 有未接线能力即 exit 1（可接 CI）

退出码：0=无；1=有（仅 --strict）；2=扫描失败。
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIR = {".git", "__pycache__", "node_modules", "runtime", "logs", ".codebuddy"}

#: 已知**有意未接线**的公开能力（附原因）。列在这里即视为"已裁决"，不再报。
#: ★ 每加一条都必须在下面写清原因 —— 否则就等于把"空转"合法化了。
KNOWN_UNWIRED: dict[str, str] = {
    # （示例位：新增能力若确属"研究侧/待准入"，登记在此并写明原因）
}


def _is_route(node: ast.AST) -> bool:
    """是否 FastAPI 路由端点（装饰器里出现 router.xxx / app.xxx）。"""
    for dec in getattr(node, "decorator_list", []):
        try:
            src = ast.unparse(dec)
        except Exception:  # noqa: BLE001
            continue
        if re.search(r"\b(router|app)\s*\.\s*(get|post|put|delete|patch)\b", src):
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="死代码（已实现未接线）巡检")
    ap.add_argument("--strict", action="store_true", help="有未接线能力即 exit 1")
    args = ap.parse_args()

    files = [p for p in ROOT.rglob("*.py")
             if not any(part in SKIP_DIR for part in p.parts)]
    texts: dict[Path, str] = {}
    for p in files:
        try:
            texts[p] = p.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
    blob = "\n".join(texts.values())

    # 收集 app/ 下模块级公开 def/class（排除路由端点）
    targets: list[tuple[str, str, int]] = []
    for p, t in texts.items():
        rel = p.relative_to(ROOT).as_posix()
        if not rel.startswith("app/"):
            continue
        try:
            tree = ast.parse(t)
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            name = node.name
            if name.startswith("_") or name == "main":
                continue
            if _is_route(node):
                continue
            targets.append((rel, name, node.lineno))

    dead = []
    for rel, name, ln in targets:
        if name in KNOWN_UNWIRED:
            continue
        n = len(re.findall(r"\b" + re.escape(name) + r"\b", blob))
        if n <= 1:
            dead.append((rel, name, ln))

    print(f"[dead] app/ 公开定义 {len(targets)} 个；已实现未接线 {len(dead)} 个")
    for rel, name, ln in sorted(dead):
        print(f"   - {rel}:{ln}  {name}()")
    if KNOWN_UNWIRED:
        print(f"[dead] 已裁决为「有意未接线」{len(KNOWN_UNWIRED)} 项：")
        for k, why in sorted(KNOWN_UNWIRED.items()):
            print(f"   · {k}：{why}")
    if not dead:
        print("[dead] ✅ 无「已实现未接线」的公开能力")
    return 1 if (dead and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
