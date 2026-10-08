"""引擎重绑定 / 连接泄漏 / 延迟绑定 —— 回归守护（2026-10-08 工单）。

三组守护，对应工单 P0-2 / P1-1 / P1-2：

1. test_rebind_affects_session_scope
   **P0-2（最重要）**：rebind_engine() 必须同时替换 _engine 与 _SessionLocal。
   sessionmaker(bind=...) 持有引擎的**当次快照**，只换引擎不换 factory ⇒ 所有走
   session_scope() 的代码（含 fee_per_lot）仍用旧库 —— 「看似生效实则无效」，
   无报错、数字照旧、只是没换库。这是本项目最典型的静默失效形态，必须钉死。

2. test_fee_per_lot_no_connection_leak
   **P1-1**：fee_per_lot 曾在 session_scope() 块**外**执行 fallback 查询，
   SQLAlchemy 2.x 下每次调用净泄漏一条连接（历史回测必走 fallback，因费率表最早
   effective_from=2026-03-11）→ 批量回测必然耗尽连接池。WB 当初上 NullPool 隔离
   的唯一理由就是这个泄漏。根因已修，本测试让「需要 NullPool 隔离」的前提被永久
   锁死：一旦失败说明泄漏回来了，**不许再用 NullPool 绕过**。

3. test_no_module_level_engine_binding
   **P1-2**：禁止任何模块在 import 时执行 engine = get_engine() 或
   SessionLocal = sessionmaker(bind=get_engine())。这类模块级求值把引擎快照烧进
   模块全局，使**任何**重绑定方式（包括官方 rebind_engine）对它失效。
   用 AST 而非正则判定，避免误报注释/字符串里的同名调用。
"""
from __future__ import annotations

import ast
import datetime as dt
import os
import pathlib

import pytest
from sqlalchemy import text

from app.core import db as dbmod
from app.core.db import get_engine, get_session_factory, rebind_engine, session_scope

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"


def _pg_backend_conns() -> int:
    """当前库上本应用的连接数（验证不随调用次数增长）。"""
    with session_scope() as s:
        return int(s.execute(text(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )).scalar() or 0)


# ---------------------------------------------------------------------------
# P0-2：重绑定必须原子替换 _engine 与 _SessionLocal
# ---------------------------------------------------------------------------
def test_rebind_affects_session_scope():
    """重绑定后 session_scope() 必须用新引擎（工单 P0-2 验收）。"""
    e1 = get_engine()
    f1 = get_session_factory()
    assert f1.kw["bind"] is e1, "前置：factory 初始应绑定当前引擎"

    url = os.environ["DATABASE_URL"]
    e2 = rebind_engine(url, dispose_old=False)

    assert e2 is not e1, "rebind_engine 应返回新引擎实例"
    assert get_engine() is e2, "get_engine() 应返回新引擎"
    # ★ 这行才是重点：只换 _engine 不换 _SessionLocal 时这里会失败
    assert get_session_factory().kw["bind"] is e2, (
        "sessionmaker 仍绑定旧引擎 —— 重绑定静默失效（P0-2）")
    with session_scope() as s:
        assert s.get_bind().engine is e2, "session_scope 实际使用了错误的引擎"
        assert s.get_bind().engine is not e1

    rebind_engine(url, dispose_old=True)  # 还原


def test_rebind_reuses_settings_when_url_none():
    """url=None 时按当前 settings 重建，且仍要同步 factory。"""
    e1 = rebind_engine(None, dispose_old=True)
    assert get_session_factory().kw["bind"] is e1
    assert get_engine() is e1


def test_rebind_pool_params_applied():
    """池参数可编程覆盖（隧道场景建议 pool_size=8 / max_overflow=16）。"""
    url = os.environ["DATABASE_URL"]
    try:
        e = rebind_engine(url, pool_size=3, max_overflow=7, dispose_old=True)
        assert e.pool.size() == 3
        assert e.pool._max_overflow == 7
    finally:
        rebind_engine(url, dispose_old=True)


def test_pool_params_env_override():
    """P1-3：QH_PG_POOL_SIZE / QH_PG_MAX_OVERFLOW 环境变量可覆盖池参数。"""
    from app.core.config import DbYAML

    os.environ["QH_PG_POOL_SIZE"] = "8"
    os.environ["QH_PG_MAX_OVERFLOW"] = "16"
    try:
        d = DbYAML(url_env="DATABASE_URL", pool_size=5, max_overflow=10, echo=False)
        assert d.pool_size == 8
        assert d.max_overflow == 16
    finally:
        os.environ.pop("QH_PG_POOL_SIZE", None)
        os.environ.pop("QH_PG_MAX_OVERFLOW", None)

    d2 = DbYAML(url_env="DATABASE_URL", pool_size=5, max_overflow=10, echo=False)
    assert d2.pool_size == 5 and d2.max_overflow == 10

# ---------------------------------------------------------------------------
# P1-1：连接泄漏回归（让 NullPool 隔离补丁永久退役）
# ---------------------------------------------------------------------------
@pytest.mark.slow
def test_fee_per_lot_no_connection_leak():
    """循环 N>=500 次调用 fee_per_lot，断言连接**不随 N 增长**（工单 P1-1）。

    刻意用 2015-06-01：费率表最早 effective_from=2026-03-11，故必然走 fallback
    分支 —— 正是当年泄漏发生的那条路径。
    """
    from app.data.cost import clear_fee_cache, fee_per_lot

    clear_fee_cache()
    sym, date, n = "RB888", dt.date(2015, 6, 1), 500

    fee_per_lot(sym, "OPEN", on_date=date)      # 预热（解析品种等一次性状态）
    before = _pg_backend_conns()
    base_checkedout = get_engine().pool.checkedout()

    for _ in range(n):
        fee_per_lot(sym, "OPEN", on_date=date)
        clear_fee_cache()                       # 强制每次真查库

    after = _pg_backend_conns()
    checkedout = get_engine().pool.checkedout()

    assert checkedout <= max(base_checkedout, 1), (
        f"连接池 checkedout={checkedout}（基线 {base_checkedout}），{n} 次调用后"
        f"仍有连接未归还 —— 泄漏回归")
    assert after <= before + 2, (
        f"后端连接数 {before} -> {after}，{n} 次调用后仍在增长 —— 泄漏回归。"
        f"注意：不要用 NullPool 绕过，先修泄漏。")


# ---------------------------------------------------------------------------
# P1-2：延迟绑定守护（禁止 import 时的模块级引擎求值）
# ---------------------------------------------------------------------------
def _module_level_engine_bindings(path: pathlib.Path):
    """返回该文件在模块级求值引擎绑定的违规 (行号, 说明)。

    只扫 tree.body（模块顶层）；def/class 内部**不扫** —— 函数体内调用
    get_engine() 是**正确**写法（运行时解析），不可误报。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    hits = []
    for node in tree.body:
        targets, value = [], None
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets, value = [node.target.id], node.value
        if not targets or value is None:
            continue
        for sub in ast.walk(value):
            if not isinstance(sub, ast.Call):
                continue
            fn = sub.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name in ("get_engine", "create_engine", "sessionmaker"):
                hits.append((sub.lineno, f"{targets[0]} = ...{name}(...) @模块级"))
                break
    return hits


def test_no_module_level_engine_binding():
    """禁止模块级 engine = get_engine() / sessionmaker(bind=get_engine())。

    这类求值把引擎快照烧进模块全局，使任何重绑定方式对它失效（P1-2）。
    白名单：app/core/db.py 自身持有 _engine/_SessionLocal 是**定义**而非违规绑定。
    """
    offenders = []
    for py in sorted(APP_DIR.rglob("*.py")):
        if py.name == "db.py":
            continue
        for lineno, why in _module_level_engine_bindings(py):
            offenders.append(f"{py.relative_to(REPO_ROOT)}:{lineno} {why}")
    assert not offenders, (
        "发现模块级引擎求值（会使 rebind_engine 静默失效）：\n  "
        + "\n  ".join(offenders)
        + "\n修法：改成函数体内调用 get_engine()（运行时解析）。")