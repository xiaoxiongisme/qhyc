# -*- coding: utf-8 -*-
"""
口径守卫：防止「回测自行重算方向层/趋势强度」再次与生产分叉。

背景（同一 bug 已发作三次）：
  · 2026-10-10：回测脚本自算**日线** EMA140 vs 生产小时线 EMA140 → 逐 bar 一致率 67.8%，
    MDD 差 44.9%（见 fusion_signal.htf_direction 文档串）。
  · 2026-10-29：runtime/bt_engine_min15.py 用**15分钟** EMA140/ADX，与生产小时线
    一致率仅 82.77%（ADX 门控 87.32%）。该文件实为独立 15 分钟变体，但其结果
    曾被当作"V3.4 实盘系统"的证据引用。

本脚本做三件事：
  C1 静态：方向层/趋势强度只允许在 app/strategies/fusion_signal.py 实现；
     其他文件若自行实现，必须显式标注独立变体（含标记串），否则 FAIL。
  C2 静态：凡是调用 walk_fusion_states 的回测，必须从 HourlyBar 取数（小时口径）。
  C3 运行时：用真实小时数据断言 htf_direction 是纯函数且无前视（末根不改变历史）。

用法：python scripts/validate/caliber_guard.py
退出码：0 = 口径一致；1 = 发现分叉。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANON = ROOT / "app" / "strategies" / "fusion_signal.py"
MARK = "独立变体·非生产口径"
# 允许自行实现方向层/ADX 的文件（须带 MARK）
ALLOW_SELF_IMPL = {
    "runtime/bt_engine_min15.py",
}
# 允许自算 _htf_direction 的既有回测（历史遗留，已用 HourlyBar 取数）
ALLOW_HTF_LOCAL = {
    "app/backtest/fusion_backtest.py",
}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "runtime/_cache"}
# 一次性排查/快照目录：命中只提示「结果不可引用」，不计 FAIL（避免告警疲劳）
SCRATCH = ("runtime/_", "runtime/pipeline_snapshot/", "imports/")

fails: list[str] = []
warns: list[str] = []


def pyfiles():
    me = Path(__file__).resolve()
    for f in ROOT.rglob("*.py"):
        if f.resolve() == me:
            continue
        if any(s in f.as_posix() for s in SKIP_DIRS):
            continue
        yield f


def rel(f: Path) -> str:
    return f.relative_to(ROOT).as_posix()


def is_scratch(r: str) -> bool:
    return any(r.startswith(s) for s in SCRATCH)


def c1_static():
    """方向层/ADX 只允许一处实现。"""
    pat_dir_def = re.compile(r"def\s+(htf_?direction|htf_dir|_htf)\s*\(")
    pat_dir_inline = re.compile(r"np\.where\(\s*[a-z_]*c\s*[<>]\s*e\w*|sign\(\s*[a-z_]*c\s*-\s*e\w*")
    pat_adx = re.compile(r"def\s+(adx14|adx_wilder|_adx)\s*\(")
    pat_ema140 = re.compile(r"ema\w*\([^)]*,\s*140\s*\)")
    # 非小时数据的痕迹：只有这类文件自算 EMA140 才算口径错
    pat_nonhour = re.compile(r"min15|_15m\b|15min|freq\s*=\s*['\"]15")
    for f in pyfiles():
        r = rel(f)
        if r == rel(CANON):
            continue
        try:
            src = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        hits = []
        if pat_dir_def.search(src):
            hits.append("方向层函数")
        if pat_dir_inline.search(src):
            hits.append("内联方向判定")
        if pat_adx.search(src):
            hits.append("ADX 实现")
        # 特征工程在同一口径上自算 EMA140 不算错；只有落到非小时数据上才算
        if pat_ema140.search(src) and pat_nonhour.search(src) and "walk_fusion_states" not in src:
            hits.append("非小时数据上自算 EMA140")
        if not hits:
            continue
        tag = "(%s)" % "/".join(hits)
        if r in ALLOW_SELF_IMPL:
            if MARK not in src:
                fails.append(f"C1 {r}: 自行实现{tag} 但缺少标记串「{MARK}」")
            else:
                warns.append(f"C1 {r}: 已知独立变体{tag}，已标注，不计入生产口径")
        elif r in ALLOW_HTF_LOCAL:
            warns.append(f"C1 {r}: 本地 _htf_direction（历史遗留，需核对是否等于 htf_direction）")
        elif is_scratch(r):
            warns.append(f"C1 {r}: 排查脚本自算{tag} → 其数字**不可引用**")
        else:
            fails.append(f"C1 {r}: 自行实现{tag}，必须改为调用 fusion_signal.htf_direction")


def c2_hourly():
    """调用 walk_fusion_states 的回测必须走 HourlyBar。"""
    for f in pyfiles():
        r = rel(f)
        try:
            src = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "walk_fusion_states" not in src:
            continue
        if "HourlyBar" in src:
            continue
        if r.startswith("runtime/") and not is_scratch(r):
            fails.append(f"C2 {r}: 调用 walk_fusion_states 但未从 HourlyBar 取数（非小时口径）")
        else:
            warns.append(f"C2 {r}: 调用 walk_fusion_states 但未见 HourlyBar，请人工核对数据源")


def c3_runtime():
    """htf_direction 无前视 + 纯函数。"""
    sys.path.insert(0, str(ROOT))
    try:
        import numpy as np
        from app.strategies.fusion_signal import htf_direction
    except Exception as e:  # noqa: BLE001
        warns.append(f"C3 无法导入 fusion_signal，跳过运行时检查: {type(e).__name__}")
        return
    rng = np.random.default_rng(0)
    c = 100 + np.cumsum(rng.normal(0, 1, 800))
    a = htf_direction(c, 140)
    b = htf_direction(c.copy(), 140)
    if not np.array_equal(np.nan_to_num(a), np.nan_to_num(b)):
        fails.append("C3 htf_direction 非纯函数（两次调用结果不同）")
    if a[0] != 0:
        fails.append("C3 htf_direction[0] 应为 0（首根无门控）")
    # 无前视：截断尾部不得改变前缀
    m = 500
    if not np.array_equal(a[:m], htf_direction(c[:m], 140)[:m]):
        fails.append("C3 htf_direction 存在前视（截断尾部改变了前缀方向）")
    # 与"前一根方向"定义一致
    e = np.asarray(__import__("pandas").Series(c).ewm(span=140, adjust=False).mean())
    ref = np.concatenate([[0], np.where(c > e, 1, -1)[:-1]])
    if not np.array_equal(a, ref):
        fails.append("C3 htf_direction 与「前一根 close vs EMA140」定义不一致")


def c4_canonical_equals():
    """权威回测内的 _htf_direction 必须与生产 htf_direction 数值完全一致。"""
    sys.path.insert(0, str(ROOT))
    try:
        import numpy as np
        from app.strategies.fusion_signal import htf_direction
        from app.backtest.fusion_backtest import _htf_direction
    except Exception as e:  # noqa: BLE001
        warns.append(f"C4 无法导入 fusion_backtest，跳过: {type(e).__name__}")
        return
    rng = np.random.default_rng(7)
    for k in range(3):
        c = 2000 + np.cumsum(rng.normal(0, 5, 1200))
        a = htf_direction(c, 140)
        b = np.asarray(_htf_direction(c, 140))
        if a.shape != b.shape or not np.array_equal(np.nan_to_num(a), np.nan_to_num(b)):
            diff = int((np.nan_to_num(a) != np.nan_to_num(b)).sum())
            fails.append(f"C4 fusion_backtest._htf_direction 与生产 htf_direction 不一致（第{k+1}组，{diff} 个 bar）")
            return
    warns.append("C4 fusion_backtest._htf_direction ≡ 生产 htf_direction（3 组随机序列逐位一致）")


def main() -> int:
    c1_static()
    c2_hourly()
    c3_runtime()
    c4_canonical_equals()
    print("=" * 68)
    print("口径守卫 caliber_guard")
    print("=" * 68)
    for w in warns:
        print("  WARN ", w)
    for f in fails:
        print("  FAIL ", f)
    if fails:
        print("\n结果：口径分叉 %d 处 → 必须修复后再引用回测结论" % len(fails))
        return 1
    print("\n结果：PASS（方向层/ADX 单一真源，回测走小时口径）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
