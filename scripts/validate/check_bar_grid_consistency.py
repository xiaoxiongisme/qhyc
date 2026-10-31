#!/usr/bin/env python3
"""一致性守卫：bar 网格在「增量合成」与「全量重建」两侧必须逐位相同。

背景（2026-10-31 事故）
----------------------
同一份「同花顺固定网格」被写在两处：

1. ``app/ingest/synthesizer.py::_SESSION_DAY_BOUNDS`` —— **增量**合成，每天随
   采集跑；之前还受 ``_SESSION_BARS`` 控制，未登记的表会**静默退回自然整点桶**。
2. ``scripts/validate/rebuild_bars_session.py::DAY_BOUNDS`` —— **全量**重建。

二者一旦分叉，表现极其隐蔽：

* 30m 增量侧漏了边界 ``345``（14:45）→ 14:15~15:00 被合成一根 45 分钟 bar；
* 5m / 15m 压根没登记进 ``_SESSION_BARS`` → 全量修好后，**下一次增量又用
  time_bucket 把它改回 13:30 / 10:30**，两方无限拉锯。

这类错误不会报错、行数也看着正常，只能靠常量比对提前拦住。本守卫做三件事：

* C1 两侧 ``DAY_BOUNDS`` 字典逐 key / 逐元素相同；
* C2 合成侧 ``_SESSION_BARS`` 覆盖 ``_BARS`` 全部表（防静默退回整点桶）；
* C3 两侧步长都能被日盘交易会话整除性自洽——即 5/15 能整除上午 135 分钟、
  30/60 不能（这正是「午盘首根不统一为 13:45」的根因，用来锁死规则理解）。

退出码：0 = 一致；非 0 = 发现分歧（可直接挂 crontab / 每日巡检）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# (相对仓库根的路径, 该文件中的网格常量名)
_GRID_TARGETS = [
    ("app/ingest/synthesizer.py", "_SESSION_DAY_BOUNDS"),
    ("scripts/validate/rebuild_bars_session.py", "DAY_BOUNDS"),
]

#: 日盘单日累计交易分钟：09:00-10:15(75) + 10:30-11:30(60) + 13:30-15:00(90)
_DAY_MINUTES = 225
#: 上午这一段（到第一节休息前的全部交易分钟）
_AM_MINUTES = 135


def _repo_root() -> Path:
    """向上找到含 ``app`` 与 ``scripts`` 的仓库根。"""
    here = Path(__file__).resolve()
    for cand in (here.parent, *here.parents):
        if (cand / "app").is_dir() and (cand / "scripts").is_dir():
            return cand
    raise SystemExit("check_bar_grid_consistency: 未定位到仓库根")


def _load_const(path: Path, name: str) -> dict:
    """用 AST 取模块级常量字面量，避免 import app（会拖起 DB 依赖）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        target = None
        value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            if isinstance(node.targets[0], ast.Name):
                target, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        if target == name and value is not None:
            try:
                return ast.literal_eval(value)
            except ValueError as exc:  # pragma: no cover - 常量写成非字面量
                raise SystemExit(f"{path}: {name} 不是字面量字典: {exc}") from exc
    raise SystemExit(f"{path}: 未找到常量 {name}")


def _load_bars_tables(path: Path) -> list[str]:
    """取 synthesizer 的 ``_BARS`` 里登记的表名列表。"""
    const = _load_const(path, "_BARS")
    return [t for t, _iv in const]


def _session_bars_of_synth(path: Path) -> dict:
    """取 synthesizer 的 ``_SESSION_BARS`` 映射。注意它带类型注解 + 多行字典。"""
    return _load_const(path, "_SESSION_BARS")


def main() -> int:
    root = _repo_root()
    problems: list[str] = []

    # ---- C1 两侧网格字典逐位相同 ----
    loaded: dict[str, dict] = {}
    for rel, name in _GRID_TARGETS:
        path = root / rel
        if not path.exists():
            problems.append(f"C1 源文件不存在: {rel}")
            continue
        loaded[f"{rel}::{name}"] = _load_const(path, name)

    if len(loaded) == 2:
        (k1, v1), (k2, v2) = list(loaded.items())
        if set(v1) != set(v2):
            problems.append(
                f"C1 步长集合不同: {k1}={sorted(v1)} vs {k2}={sorted(v2)}")
        else:
            for step in sorted(v1):
                if list(v1[step]) != list(v2[step]):
                    only_a = set(v1[step]) - set(v2[step])
                    only_b = set(v2[step]) - set(v1[step])
                    problems.append(
                        f"C1 step={step} 边界不同: {k1}独有={sorted(only_a)} "
                        f"{k2}独有={sorted(only_b)}")
        steps = sorted(loaded[f"{_GRID_TARGETS[0][0]}::{_GRID_TARGETS[0][1]}"])
    else:
        steps = []

    # ---- C2 合成侧必须覆盖全部 bar 表（防静默退回整点桶）----
    synth = root / "app/ingest/synthesizer.py"
    if synth.exists():
        try:
            bars = _load_bars_tables(synth)
            sess = _session_bars_of_synth(synth)
        except SystemExit as exc:
            problems.append(f"C2 读取 _BARS/_SESSION_BARS 失败: {exc}")
        else:
            missing = [t for t in bars if t not in sess]
            if missing:
                problems.append(
                    f"C2 以下表未登记会话式切分，会退回自然整点桶: {missing}")

    # ---- C3 规则自洽性：锁定「午盘首根不统一 13:45」的正确理解 ----
    # 上午 135 分钟能被 step 整除 → 下午从新 marker 开始（5m→13:35, 15m→13:45）
    # 不能整除 → 余段跨午休并入下午首根（30m→13:45, 60m→14:15）
    expected_divisible = {5: True, 15: True, 30: False, 60: False}
    for step in steps:
        if step not in expected_divisible:
            continue
        divisible = (_AM_MINUTES % step == 0)
        if divisible != expected_divisible[step]:
            problems.append(
                f"C3 step={step} 整除性断言失败: 135%{step}==0 为 {divisible}，"
                f"与既定网格规则不符（会改变午盘首根锚点）")

    # C3b 最后一根必须收在 15:00（相对 09:00 = 360 分钟墙钟偏移）
    base = f"{_GRID_TARGETS[0][0]}::{_GRID_TARGETS[0][1]}"
    for step in steps:
        bounds = loaded[base][step]
        if bounds and bounds[-1] != 360:
            problems.append(f"C3b step={step} 末边界={bounds[-1]}，应为 360(15:00)")

    if problems:
        print("[FAIL] bar 网格一致性校验未通过:")
        for p in problems:
            print("  -", p)
        return 1

    print("[OK] bar 网格一致：")
    for key, val in loaded.items():
        summary = ", ".join(f"{k}m({len(v)}根)" for k, v in sorted(val.items()))
        print(f"  {key} -> {summary}")
    print("  C2 全部 bar_* 表均已登记会话式切分")
    print("  C3 午盘首根规则自洽：5m=13:35 / 15m=13:45 / 30m=13:45 / 60m=14:15")
    return 0


if __name__ == "__main__":
    sys.exit(main())
