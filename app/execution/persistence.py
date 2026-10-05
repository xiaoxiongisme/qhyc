# -*- coding: utf-8 -*-
"""P0-2 Sprint 1 · 持久层：**唯一**写库出口（禁止裸 SQL 散落）。

对应 DDL：migrations/030_execution_channels.sql
承载全项目最重的一条防错：**idempotency_key UNIQUE 防重复下单**（真实资金事故）。

纪律（沿用项目 fail-loud 传统）：
  * 状态落 ERROR/REJECTED 必须带 last_error，否则**直接抛错**，不静默写空；
  * 幂等命中返回旧 id，绝不新下第二单；
  * 缺字段即报错，不按 0 / 默认值兜底掩盖缺失。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from app.core.db import session_scope

#: 幂等键格式：同一信号 + 同一真实合约 + 同一动作 + 同一交易日 = 只允许一单
IDEMPOTENCY_FMT = "{signal_id}:{real_symbol}:{action}:{yyyymmdd}"

#: 状态机允许迁移（DB CHECK 固化第一层，这里做第二层应用侧断言）
_TRANSITIONS: dict[str, set[str]] = {
    "NEW": {"SENT", "REJECTED", "ERROR", "CANCELED"},
    "SENT": {"PARTIAL", "FILLED", "CANCELED", "REJECTED", "ERROR"},
    "PARTIAL": {"PARTIAL", "FILLED", "CANCELED", "ERROR"},
    "FILLED": set(),
    "CANCELED": set(),
    "REJECTED": set(),
    "ERROR": {"SENT"},
}


class ExecutionStateError(RuntimeError):
    """非法状态迁移或缺失必填错误信息（fail-loud）。"""


def build_idempotency_key(signal_id: str, real_symbol: str, action: str, d: date) -> str:
    """按统一格式生成幂等键。"""
    return IDEMPOTENCY_FMT.format(
        signal_id=signal_id, real_symbol=real_symbol, action=action,
        yyyymmdd=d.strftime("%Y%m%d"),
    )


def _assert_transition(src: str, dst: str) -> None:
    if src == dst:
        return
    allowed = _TRANSITIONS.get(src)
    if allowed is None:
        raise ExecutionStateError(f"未知源状态 {src!r}（DB CHECK 应已拦住）")
    if dst not in allowed:
        raise ExecutionStateError(f"非法状态迁移 {src} → {dst}（允许：{sorted(allowed)}）")


def save_order(order: dict[str, Any]) -> int:
    """落订单；同 idempotency_key 已存在则**直接返回旧 id**（幂等命中，绝不重复下单）。

    order 需含：signal_id, symbol_continuous, real_symbol, exchange, action, direction,
    price, price_space, lots, multiplier, notional, channel, account, idempotency_key。
    """
    key = order["idempotency_key"]
    with session_scope() as s:
        rid = s.execute(
            text("SELECT id FROM execution_order WHERE idempotency_key = :k"), {"k": key}
        ).scalar()
        if rid:
            return int(rid)
        return int(
            s.execute(
                text(
                    """
                    INSERT INTO execution_order
                        (signal_id, symbol_continuous, real_symbol, exchange,
                         action, direction, price, price_space, lots, multiplier, notional,
                         channel, account, status, idempotency_key)
                    VALUES (:signal_id, :sym, :real, :ex, :action, :dir, :px, :space, :lots,
                            :mult, :notional, :channel, :account, 'NEW', :key)
                    RETURNING id
                    """
                ),
                {
                    "signal_id": order.get("signal_id"),
                    "sym": order["symbol_continuous"],
                    "real": order["real_symbol"],
                    "ex": order.get("exchange"),
                    "action": order["action"],
                    "dir": order["direction"],
                    "px": order["price"],
                    "space": order.get("price_space", "raw"),
                    "lots": order["lots"],
                    "mult": order.get("multiplier"),
                    "notional": order.get("notional"),
                    "channel": order.get("channel", "SIM"),
                    "account": order.get("account"),
                    "key": key,
                },
            ).scalar()
        )


def get_order(order_id: int) -> dict[str, Any] | None:
    with session_scope() as s:
        row = s.execute(
            text("SELECT * FROM execution_order WHERE id = :id"), {"id": order_id}
        ).mappings().first()
        return dict(row) if row else None


def find_order_by_key(key: str) -> dict[str, Any] | None:
    with session_scope() as s:
        row = s.execute(
            text("SELECT * FROM execution_order WHERE idempotency_key = :k"), {"k": key}
        ).mappings().first()
        return dict(row) if row else None


def update_status(
    order_id: int,
    status: str,
    *,
    broker_order_id: str | None = None,
    error: str | None = None,
) -> None:
    """状态迁移。**ERROR/REJECTED 必须带 error**，否则抛错（不静默）。"""
    if status in ("ERROR", "REJECTED") and not error:
        raise ExecutionStateError(f"[fail-loud] status={status} 必须提供 error/last_error")
    with session_scope() as s:
        cur = s.execute(
            text("SELECT status FROM execution_order WHERE id = :id"), {"id": order_id}
        ).scalar()
        if cur is None:
            raise ExecutionStateError(f"订单不存在 id={order_id}")
        _assert_transition(cur, status)
        s.execute(
            text(
                """
                UPDATE execution_order
                   SET status = :st,
                       broker_order_id = COALESCE(:bid, broker_order_id),
                       last_error = :err,
                       retry_count = retry_count + CASE WHEN :st = 'ERROR' THEN 1 ELSE 0 END,
                       updated_at = now()
                 WHERE id = :id
                """
            ),
            {"st": status, "bid": broker_order_id, "err": error, "id": order_id},
        )


def save_fill(
    order_id: int,
    *,
    broker_fill_id: str,
    fill_ts: datetime,
    fill_price: Decimal | float,
    fill_lots: int,
    fee: Decimal | float | None = None,
) -> int:
    """成交回报；UNIQUE(order_id, broker_fill_id) 幂等（网关重复推送不重复记账）。"""
    with session_scope() as s:
        got = s.execute(
            text(
                """
                INSERT INTO execution_fill
                    (order_id, broker_fill_id, fill_ts, fill_price, fill_lots, fee)
                VALUES (:oid, :fid, :ts, :px, :lots, :fee)
                ON CONFLICT (order_id, broker_fill_id) DO NOTHING
                RETURNING id
                """
            ),
            {
                "oid": order_id, "fid": broker_fill_id, "ts": fill_ts,
                "px": fill_price, "lots": fill_lots, "fee": fee,
            },
        ).scalar()
        if got:
            return int(got)
        return int(
            s.execute(
                text("SELECT id FROM execution_fill WHERE order_id=:oid AND broker_fill_id=:fid"),
                {"oid": order_id, "fid": broker_fill_id},
            ).scalar()
        )


def upsert_position(
    *,
    real_symbol: str,
    net_lots: int,
    long_lots: int,
    short_lots: int,
    today_lots: int,
    avg_open_price: Decimal | float | None = None,
    channel: str = "SIM",
    account: str | None = None,
    snapshot_date: date | None = None,
) -> None:
    """持仓快照 UPSERT（G2 聚合；开平推导依赖 net/long/short/today 四字段）。"""
    d = snapshot_date or date.today()
    with session_scope() as s:
        s.execute(
            text(
                """
                INSERT INTO execution_position
                    (snapshot_date, real_symbol, channel, account,
                     net_lots, long_lots, short_lots, today_lots, avg_open_price)
                VALUES (:d, :sym, :ch, :acc, :net, :lng, :shrt, :today, :avg)
                ON CONFLICT (snapshot_date, real_symbol, channel, account)
                DO UPDATE SET net_lots = EXCLUDED.net_lots, long_lots = EXCLUDED.long_lots,
                              short_lots = EXCLUDED.short_lots, today_lots = EXCLUDED.today_lots,
                              avg_open_price = EXCLUDED.avg_open_price, updated_at = now()
                """
            ),
            {
                "d": d, "sym": real_symbol, "ch": channel, "acc": account,
                "net": net_lots, "lng": long_lots, "shrt": short_lots,
                "today": today_lots, "avg": avg_open_price,
            },
        )


def get_position(
    real_symbol: str, *, channel: str = "SIM", account: str | None = None,
    snapshot_date: date | None = None,
) -> dict[str, Any] | None:
    """取当日持仓（G2）；无记录返回 None（调用方须显式处理，不默认 0）。"""
    d = snapshot_date or date.today()
    with session_scope() as s:
        row = s.execute(
            text(
                """
                SELECT * FROM execution_position
                 WHERE snapshot_date = :d AND real_symbol = :sym
                   AND channel = :ch AND account IS NOT DISTINCT FROM :acc
                """
            ),
            {"d": d, "sym": real_symbol, "ch": channel, "acc": account},
        ).mappings().first()
        return dict(row) if row else None


def mark_submit_attempt(order_id: int) -> None:
    """记录一次**已发出但未确认**的提交尝试（超时路径）。

    用于机械阻断「同一 NEW 订单被二次提交」——幂等键只护落库，不护提交；
    超时后订单仍为 NEW，若无此标记则再次 submit 会**真的再下一次单**。
    复用 ``retry_count``（DDL 030 已有列，不新增 schema）。
    """
    with session_scope() as s:
        s.execute(
            text(
                "UPDATE execution_order SET retry_count = retry_count + 1, updated_at = now() "
                "WHERE id = :id"
            ),
            {"id": order_id},
        )


def record_health(channel: str, *, healthy: bool, latency_ms: int | None = None,
                  detail: str | None = None) -> None:
    """通道健康/延迟（monitor.py 写入，供告警）。"""
    with session_scope() as s:
        s.execute(
            text(
                """
                INSERT INTO execution_channel_health (channel, healthy, latency_ms, detail)
                VALUES (:ch, :h, :lat, :d)
                """
            ),
            {"ch": channel, "h": healthy, "lat": latency_ms, "d": detail},
        )


def list_open_orders(statuses: tuple[str, ...] = ("NEW", "SENT", "PARTIAL")) -> list[dict[str, Any]]:
    """列出未终态订单（recover_on_boot / 监控用）。"""
    with session_scope() as s:
        rows = s.execute(
            text(
                "SELECT * FROM execution_order WHERE status = ANY(:st) ORDER BY created_at"
            ),
            {"st": list(statuses)},
        ).mappings().all()
        return [dict(r) for r in rows]
