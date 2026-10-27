# -*- coding: utf-8 -*-
"""平仓流水（trade_journal）读写。

定位
----
``execution_fill`` 记的是**逐笔成交**，适合对账；本表记的是**往返级**（一次开→一次平），
适合回答三个问题：

1. 今天连亏几笔了？（→ 连亏熔断）
2. 这一笔的 R 倍数是多少，有没有扛过止损？（→ 行为违规取证）
3. 剔除最大 3 笔之后还剩多少？（→ 接受标准，归档教训 #17）

口径铁律
--------
* ``net = gross − fee``，两个字段都从账单/成交回报**原样取**，不做二次估算。
* ``r_multiple`` 只在**有计划止损**（``planned_stop`` 非空）时计算；缺止损即为
  ``NULL``，**绝不按"事后止损"倒推**（那是自欺）。
* 同一账户同一合约同一开平时刻同一手数只入一次（UNIQUE 约束保证），可重复导入。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import logger

#: 单笔亏损超过 1.2R 视为"扛过止损"（与 exit_guard.DEFAULT_R_VIOLATION 对齐）
R_VIOLATION = 1.2


@dataclass
class TradeRow:
    account: str
    real_symbol: str
    direction: str
    open_at: datetime
    close_at: datetime
    lots: int
    open_px: float
    close_px: float
    gross: float
    fee: float
    planned_stop: float | None = None
    close_reason: str | None = None
    source: str = "IMPORT"

    @property
    def net(self) -> float:
        return self.gross - self.fee

    def r_multiple(self) -> float | None:
        """净盈亏 / 计划风险。多头：风险 = (开仓−止损)×手数；空头反向。"""
        if self.planned_stop is None or self.open_px <= 0:
            return None
        if self.direction == "LONG":
            risk = (self.open_px - self.planned_stop) * self.lots
        else:
            risk = (self.planned_stop - self.open_px) * self.lots
        if risk <= 0:
            return None
        return self.net / risk

    def violated(self) -> bool:
        r = self.r_multiple()
        return bool(r is not None and r < -R_VIOLATION)


INSERT_SQL = text("""
INSERT INTO trade_journal
    (account, real_symbol, direction, open_at, close_at, lots, open_px, close_px,
     gross, fee, net, planned_stop, r_multiple, close_reason, violated, source)
VALUES
    (:account, :real_symbol, :direction, :open_at, :close_at, :lots, :open_px, :close_px,
     :gross, :fee, :net, :planned_stop, :r_multiple, :close_reason, :violated, :source)
ON CONFLICT (account, real_symbol, open_at, close_at, lots) DO NOTHING
""")


def upsert(session: Session, rows: Iterable[TradeRow]) -> tuple[int, int]:
    """批量写入，返回 ``(尝试数, 实际新增数)``。冲突即跳过（可重复导入）。"""
    tried = added = 0
    for r in rows:
        if r.lots <= 0:
            continue
        tried += 1
        rm = r.r_multiple()
        res = session.execute(INSERT_SQL, {
            "account": r.account, "real_symbol": r.real_symbol,
            "direction": r.direction, "open_at": r.open_at, "close_at": r.close_at,
            "lots": r.lots, "open_px": r.open_px, "close_px": r.close_px,
            "gross": r.gross, "fee": r.fee, "net": r.net,
            "planned_stop": r.planned_stop, "r_multiple": rm,
            "close_reason": r.close_reason, "violated": r.violated(),
            "source": r.source,
        })
        added += max(0, res.rowcount or 0)
    logger.info(f"[trade_journal] 尝试 {tried} 笔，新增 {added} 笔")
    return tried, added


def consecutive_losses(session: Session, account: str, trade_date) -> int:
    """截至 ``trade_date`` 当日，从最近一笔往前数连续亏损笔数。"""
    rows = session.execute(text(
        "SELECT net FROM trade_journal WHERE account = :a AND close_at::date = :d "
        "ORDER BY close_at DESC"), {"a": account, "d": trade_date}).all()
    n = 0
    for (net,) in rows:
        if net <= 0:
            n += 1
        else:
            break
    return n


def streak_profile(session: Session, account: str | None = None) -> dict[str, Any]:
    """连亏串画像：按「此前已连亏 k 笔」分组看下一笔表现。

    这是判断"该不该在连亏后继续做"的唯一证据形式（实测：连亏 1 笔后下一笔
    胜率从 78.9% 掉到 56.5%，均净从 −6 元掉到 −426 元）。
    """
    sql = "SELECT account, net, close_at FROM trade_journal"
    params: dict[str, Any] = {}
    if account:
        sql += " WHERE account = :a"
        params["a"] = account
    sql += " ORDER BY close_at"
    rows = session.execute(text(sql), params).all()
    if not rows:
        return {}
    buckets: dict[int, list[float]] = {}
    streak = 0
    for _, net, _ in rows:
        k = min(streak, 4)
        buckets.setdefault(k, []).append(float(net))
        streak = streak + 1 if net <= 0 else 0
    out: dict[str, Any] = {}
    for k in sorted(buckets):
        v = buckets[k]
        out[f"前{k}笔连亏"] = {
            "n": len(v), "net": round(sum(v), 2),
            "win_rate": round(sum(1 for x in v if x > 0) / len(v), 4),
            "avg": round(sum(v) / len(v), 2),
        }
    return out


def violations(session: Session, account: str | None = None) -> dict[str, Any]:
    """统计有计划的交易里，有多少是扛过止损 / 提前离场。"""
    sql = ("SELECT account, real_symbol, close_at, r_multiple, net, close_reason "
           "FROM trade_journal WHERE r_multiple IS NOT NULL")
    params: dict[str, Any] = {}
    if account:
        sql += " AND account = :a"
        params["a"] = account
    rows = session.execute(text(sql), params).all()
    if not rows:
        return {"n_with_plan": 0}
    over = [r for r in rows if float(r[3]) < -R_VIOLATION]
    early = [r for r in rows if float(r[3]) > 0 and (r[5] or "") == "MANUAL"]
    return {
        "n_with_plan": len(rows),
        "n_over_stop": len(over),
        "over_stop_net": round(sum(float(r[4]) for r in over), 2),
        "n_early_exit": len(early),
        "mean_r": round(sum(float(r[3]) for r in rows) / len(rows), 3),
    }


def recent(session: Session, account: str, limit: int = 50) -> Sequence[Any]:
    return session.execute(text(
        "SELECT real_symbol, direction, open_at, close_at, lots, open_px, close_px, "
        "       net, r_multiple, close_reason, violated "
        "FROM trade_journal WHERE account = :a ORDER BY close_at DESC LIMIT :n"),
        {"a": account, "n": limit}).all()


__all__ = ["TradeRow", "upsert", "consecutive_losses", "streak_profile",
           "violations", "recent", "R_VIOLATION"]
