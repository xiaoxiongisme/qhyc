"""交易品种口径唯一真源（CL-20261107-01 S2/S3/S4）。

本模块回答一个问题：**哪些 symbol 应该进入加工层（L1/L2）与兼容视图？**

口径定义
--------
    派生/可见集合 = 有效白名单（l3_ref.tradable_variety）
                  ∪ 持仓豁免（public.fusion_position 中 position <> 'FLAT'）
                  ∪ 开关关闭时 = 全部已知品种（秒回退）

三段式定义全部收敛在数据库视图 ``l3_ref.v_tradable_scope`` 里，Python 侧只读
这个视图，SQL 侧（存储过程、兼容视图）也只读这个视图。**口径只有一份**——
这是 2026-11-07 之前四套口径并存（白名单 38 / cloud.yaml 50 / L1 主连 82 /
融合信号 41）事故的直接对策。

为什么必须含「持仓豁免」
------------------------
改动前实测：B888 / J888 / LH888 / NI888 四个非白名单品种**仍有持仓**。
若 L1 一刀切停派生，它们不再有新 bar，而持仓管理（止损/平仓判定）完全依赖
最新 bar —— 持仓会被永久冻结在最后一个价格上，风控形同虚设。
语义是「不再开新仓，但存量持仓必须管理到平仓为止」。

为什么必须 fail-open
--------------------
宁可多派生，也不能因为一张参考表读不到就让整条数据链停机。所有读取路径
异常/空集合时一律返回 ``None``（= 不过滤），并打 WARNING。
代价只是多算一些无用品种，不会丢数据。

为什么必须可秒回退
------------------
``cfg_feature_switch.tradable_filter_enabled = f`` 即恢复全量派生/全量可见，
不需要改代码、不需要重启进程（视图是运行期求值，SP 每次 CALL 重新读）。

与 v3 方案「L0 全采不动」的关系
------------------------------
本模块只作用于**加工层与读取层**。``l0_raw.minute_bar`` 对所有品种全量采集，
不经本模块过滤 —— 所以被停派生的品种随时可以「改白名单 + 重算」恢复，
不存在不可逆的数据丢失。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.core.logging import logger

#: 口径视图名（唯一真源）。改动它之前请先改 migrations/053 并同步此处。
SCOPE_VIEW = "l3_ref.v_tradable_scope"

_SUFFIX_RE = re.compile(r"8888?$")


def variety_of(symbol: str) -> str:
    """``RB888`` / ``RB8888`` → ``RB``；``rb2601`` → ``rb2601``（合约不动）。"""
    return _SUFFIX_RE.sub("", symbol)


@dataclass(frozen=True)
class Scope:
    """:attr:`symbols` 为 ``None`` 表示「无法判定口径」→ 调用方必须 fail-open。"""

    symbols: frozenset[str] | None
    reason: str

    @property
    def active(self) -> bool:
        """True = 真的在收口；False = fail-open（不过滤）。"""
        return self.symbols is not None

    def allows(self, symbol: str) -> bool:
        return self.symbols is None or symbol in self.symbols

    def __len__(self) -> int:  # 便于日志
        return len(self.symbols or ())


_FAIL_OPEN = Scope(None, "fail-open")


def load_scope(bind: Engine | Session | Connection) -> Scope:
    """读取当前口径。**任何异常都降级为 fail-open**，绝不抛出。

    调用方拿到的 :class:`Scope` 若是 ``active=False``，必须跳过过滤。
    """
    try:
        if isinstance(bind, Session):
            rows = bind.execute(text(f"SELECT symbol FROM {SCOPE_VIEW}"))
        elif isinstance(bind, Connection):
            rows = bind.execute(text(f"SELECT symbol FROM {SCOPE_VIEW}"))
        else:  # Engine
            with bind.connect() as conn:
                rows = conn.execute(text(f"SELECT symbol FROM {SCOPE_VIEW}"))
        syms = frozenset(r[0] for r in rows)
    except Exception as exc:  # noqa: BLE001 —— 见模块 docstring「为什么必须 fail-open」
        logger.warning(
            f"[scope] 口径视图 {SCOPE_VIEW} 读取失败 → 本轮不过滤（fail-open）: {exc}")
        return Scope(None, f"读取异常: {type(exc).__name__}")

    if not syms:
        logger.warning(
            f"[scope] 口径视图 {SCOPE_VIEW} 为空 → 本轮不过滤"
            "（fail-open，避免整条数据链零产出）")
        return Scope(None, "空集合")

    return Scope(syms, "ok")


def log_scope(scope: Scope, where: str) -> None:
    """统一口径日志：让「这次到底过滤了没有」在日志里可查，不靠猜。"""
    if not scope.active:
        logger.warning(f"[scope] {where}: 未收口（{scope.reason}）→ 全品种处理")
        return
    logger.info(f"[scope] {where}: 收口到 {len(scope)} 个 symbol")


def scope_params(scope: Scope) -> dict:
    """SQL 参数：``{"scope": [...]}``；fail-open 时给空列表并配合 :func:`where_clause`。"""
    return {"scope": sorted(scope.symbols) if scope.symbols else []}


def where_clause(scope: Scope, symbol_expr: str = "symbol",
                 param: str = "scope") -> str:
    """返回可直接拼进 WHERE 的片段（含前导 ``AND``），fail-open 时返回空串。

    用法::

        sql = f"SELECT ... FROM minute_bar WHERE ts >= :since {where_clause(scope)}"
        params = {"since": since, **scope_params(scope)}
    """
    if not scope.active:
        return ""
    return f" AND {symbol_expr} = ANY(:{param})"


def describe() -> str:
    """给运维/守卫脚本用的一行摘要（不查库）。"""
    return f"口径 = {SCOPE_VIEW}（白名单 ∪ 持仓豁免；开关 tradable_filter_enabled 关闭即全量）"


#: 回测/交易池的唯一真源表（**纯白名单，不含持仓豁免**）。
#: 与 SCOPE_VIEW 的区别见下方 load_tradable_pool 的 docstring。
TRADABLE_TABLE = "l3_ref.tradable_variety"


def load_tradable_pool(bind: Engine | Session | Connection) -> frozenset[str]:
    """读取**可交易品种码**（38 个），供回测/研究/交易池使用。

    与 :func:`load_scope` 的两处关键差异（用户 2026-11-08 19:48 拍板）：

    ==================  ==========================  ==========================
                        load_scope                  load_tradable_pool
    ==================  ==========================  ==========================
    集合               白名单 ∪ 持仓豁免（80 sym）  纯白名单（38 品种码）
    用途               L1/L2 加工、兼容视图          回测 / 研究 / 交易池
    失败语义           **fail-open**（多派生无害）   **fail-closed**（抛错）
    ==================  ==========================  ==========================

    为什么这里必须 fail-closed：回测若在口径读不到时悄悄退回
    ``settings.main_contracts``（50+ 主连，含非白名单品种），就会用错误的
    品种池产出结论，而这类错误**看起来完全正常**。宁可不跑，不能跑错池。
    持仓豁免（B/J/LH/NI）只保留在数据链路供存量持仓拿最新 bar 与止损价，
    **不进回测与交易池**。

    Returns:
        ``frozenset`` of 大写品种码，如 ``{"A", "AO", ..., "Y"}``。

    Raises:
        RuntimeError: 视图/表读取异常或有效白名单为空。
    """
    sql = text(
        f"SELECT upper(variety_code) FROM {TRADABLE_TABLE} "
        "WHERE effective_from <= current_date "
        "  AND (effective_to IS NULL OR effective_to > current_date)")
    try:
        if isinstance(bind, Session):
            rows = bind.execute(sql)
        elif isinstance(bind, Connection):
            rows = bind.execute(sql)
        else:  # Engine
            with bind.connect() as conn:
                codes = frozenset(r[0] for r in conn.execute(sql))
                if not codes:
                    raise RuntimeError("有效白名单为空")
                return codes
        codes = frozenset(r[0] for r in rows)
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"可交易白名单 {TRADABLE_TABLE} 读取失败 → 回测拒绝执行（fail-closed）: "
            f"{type(exc).__name__}: {exc}") from exc
    if not codes:
        raise RuntimeError(f"有效白名单 {TRADABLE_TABLE} 为空 → 回测拒绝执行")
    return codes
