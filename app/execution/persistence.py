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
from app.core.logging import logger

#: 幂等键格式：同一信号 + 同一真实合约 + 同一动作 + 同一交易日 = 只允许一单
IDEMPOTENCY_FMT = "{signal_id}:{real_symbol}:{action}:{yyyymmdd}"

#: 撞键时的**作用域扩展键**（仅在同键分属不同账户/通道时启用）。
#:
#: ★ 2026-11-06 新增。根因：:data:`IDEMPOTENCY_FMT` **不含 account/channel**，
#:   而 UNIQUE(idempotency_key) 是全局唯一 → 同一 signal_id 在**不同账户**
#:   （如 SIM 模拟盘 + REAL 实盘）下会撞成同一把键。于是第二个账户的单
#:   **根本没落库**，却返回了第一个账户的 order_id，调用方随后把它提交给网关
#:   ——第二个账户的仓位永远不动，且**无任何报错**（真实资金事故型缺陷）。
#:
#: 处置：命中同键但账户/通道不同 → **不复用旧单**（那会让新账户丢单），
#:   改用带作用域后缀的键落库，并记 error（撞键是异常，必须留痕）。
_SCOPED_KEY_FMT = "{key}@{channel}:{account}"

#: 终态集合（幂等/去重判定：终态单允许同信号重新下单）。
TERMINAL_STATUSES = ("FILLED", "CANCELED", "REJECTED", "ERROR")


def _scope_value(v: Any) -> str:
    """把 account/channel 归一成稳定的键片段（None → '-'）。"""
    s = "" if v is None else str(v).strip()
    return s if s else "-"


def _same_scope(row: dict[str, Any], order: dict[str, Any]) -> bool:
    """幂等键命中时，判断旧单与新单是否属**同一账户 + 同一通道**。"""
    return (
        _scope_value(row.get("account")) == _scope_value(order.get("account"))
        and _scope_value(row.get("channel")) == _scope_value(order.get("channel"))
    )


def _scoped_key(key: str, channel: Any, account: Any) -> str:
    return _SCOPED_KEY_FMT.format(
        key=key, channel=_scope_value(channel), account=_scope_value(account))

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

    ★ 2026-11-06 修正（撞键丢单缺陷）
      ------------------------------------------------
      幂等键**不含 account/channel**，而 UNIQUE(idempotency_key) 全局唯一 →
      同一 signal_id 在不同账户（SIM 模拟盘 vs REAL 实盘）下撞成同一把键。
      旧实现命中即返回旧 id ⇒ **第二个账户的单根本没落库**，却返回了第一个
      账户的 order_id，调用方随后把它提交给网关 ⇒ 第二个账户仓位永远不动，
      且**全程无报错**（静默丢单）。

      现行处置：
        · 命中且**同账户同通道** → 真幂等命中，返回旧 id（既有行为不变）；
        · 命中但**账户/通道不同** → 判定为撞键，**不复用旧单**（复用=新账户丢单），
          改用带作用域后缀的键落库，并记 error 留痕。
    """
    key = order["idempotency_key"]
    with session_scope() as s:
        hit = _find_by_key(s, key)
        if hit is not None:
            if _same_scope(hit, order):
                return int(hit["id"])
            # 撞键：同键、不同账户/通道 → 不能复用旧单
            key = _scoped_key(key, order.get("channel"), order.get("account"))
            hit2 = _find_by_key(s, key)
            if hit2 is not None:
                return int(hit2["id"])
            logger.error(
                "[exec] 幂等键撞车：key={} 已存在但账户/通道不同"
                "（旧单 id={} account={} channel={}；新单 account={} channel={}）"
                "→ 已改用作用域键落库，**未复用旧单**（复用会导致该账户丢单）".format(
                    order["idempotency_key"], hit["id"], hit.get("account"),
                    hit.get("channel"), order.get("account"), order.get("channel")))
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


def _find_by_key(s, key: str) -> dict[str, Any] | None:
    """在**给定 session** 内按幂等键查订单行（内部复用，避免嵌套 session）。

    ★ 2026-11-06：原先 :func:`find_order_by_key` 独立开 session 且无调用方（死代码），
      幂等命中判定在 :func:`save_order` 里另写了一份裸 SELECT。两处口径必须一致，
      故抽出本函数作为**唯一**的按键查询实现。
    """
    row = s.execute(
        text("SELECT * FROM execution_order WHERE idempotency_key = :k"), {"k": key}
    ).mappings().first()
    return dict(row) if row else None


def find_order_by_key(key: str) -> dict[str, Any] | None:
    """按**幂等键**查订单（只读，不改库）。

    ★ 2026-11-06 接线：此前本函数**无任何调用方**（死代码）——「能按幂等键查单」
      这个运维刚需（排查重复下单/丢单）实际上不可用。现：
        · :func:`save_order` 的幂等命中判定复用同口径查询（见 :func:`_find_by_key`）；
        · API ``GET /execution/orders/by-key`` 暴露为只读查询端点。
    """
    with session_scope() as s:
        return _find_by_key(s, key)


def find_order_by_signal(
    account: str | None,
    real_symbol: str,
    signal_id: str,
    *,
    only_unsettled: bool = False,
) -> dict[str, Any] | None:
    """按 ``(account, real_symbol, signal_id)`` 查最近一笔订单（只读，不改库）。

    为什么需要它（2026-11-06）
    --------------------------
    幂等键含**交易日**（:data:`IDEMPOTENCY_FMT`）→ 同一 signal_id **跨天**就不再幂等，
    而"同一个信号"在跨天场景下仍然只应有一单（例：止损单 signal_id 由品种+价格生成，
    同价跨天会再次平仓）。本函数提供该维度上的**只读**查重能力，
    由 API 层在下单前调用（命中未终态订单则**拒新单**，绝不改既有订单）。

    ⚠ 本函数**不做任何写操作**，也**不**替代幂等键 —— 它是幂等键之外的**第二道**
      人工/自动复核闸门，命中与否由调用方决策。
    """
    sql = (
        "SELECT * FROM execution_order "
        "WHERE real_symbol = :sym AND signal_id = :sid "
        "AND account IS NOT DISTINCT FROM :acc "
    )
    params = {"sym": real_symbol, "sid": signal_id, "acc": account}
    if only_unsettled:
        sql += "AND status <> ALL(:term) "
        params["term"] = list(TERMINAL_STATUSES)
    sql += "ORDER BY created_at DESC, id DESC LIMIT 1"
    with session_scope() as s:
        row = s.execute(text(sql), params).mappings().first()
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
