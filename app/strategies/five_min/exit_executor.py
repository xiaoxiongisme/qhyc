# -*- coding: utf-8 -*-
"""5m 模拟盘离场执行链路（CL-20261124-06，P3 闭环）。

补 two missing rings of P3 (CL-20261122-01):
  1. 开仓落单成功后，按 5M-EXIT-CALIBER 口径写一份离场计划到 ``five_min_exit_plan``；
  2. 盘中作业轮询 OPEN 计划 → exit_guard 决策 → 下 CLOSE 单（强制 SIM）→ 回写动作/关计划。

口径纪律（5M-EXIT-CALIBER 守卫钉死，勿引入 V3.4 账户级口径）
----------------------------------------------------------
* 1R = 3ATR（止损距离）；止盈 = 1.5R = 4.5ATR（CL-20261128-01 用户拍板，
  与全组合五道检验唯一幸存结构 S2 的 SL3/TP4.5 对齐）；tp1 禁用；
* 触发二选一：触 init_stop（HARD_STOP）或触 tp_px（TP），**全手数平仓**，不分批、
  不做时间止损/吊灯——那是 V3.4 exit_guard 的默认口径，与本策略研究口径不同，
  混用会污染单一真源。本模块只借 :func:`exit_guard.evaluate` 的三段判定代码，
  但喂给它的 plan dict 把 tp1/time_stop/trail 全部置空，只保留 HARD_STOP 与 TP2。

安全（与开仓链路对称）
--------------------
* 下平仓单强制走 ``/execution/sim/orders``（channel=SIM + broker=sim 双校验）；
* 作业受 ``five_min_exit_scan_enabled`` 开关 + 交易时段门 + broker==sim 门禁；
* 池级在场校验：只平本池当前持仓合约（PoolPositionGuard.check_close）；
* 一信号一计划（signal_id UNIQUE），一计划最多一成功 CLOSE（幂等 + status 状态机）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import logger


def build_exit_plan(
    *,
    signal_id: str,
    open_order_id: int | None,
    symbol_continuous: str,
    real_symbol: str,
    exchange: str,
    direction: str,
    lots: int,
    entry_px: float,
    init_stop: float,
    r_unit: float,
    tp_r: float = 1.5,
    account: str = "SIM",
    channel: str = "SIM",
    pool_id: str | None = None,
) -> dict[str, Any]:
    """按 5M-EXIT-CALIBER 构造一份离场计划（纯计算，不落库）。

    ``init_stop`` / ``r_unit`` 来自开仓意图的 exit_plan payload（1R=3ATR）；
    ``tp_r`` 固定 1.5（CL-20261128-01，tp1 禁用）。fail-loud：任何口径非法直接抛错。
    """
    d = str(direction or "").upper()
    if d not in ("LONG", "SHORT"):
        raise ValueError(f"[fail-loud] direction 必须 LONG/SHORT，收到 {direction!r}")
    if int(lots) <= 0:
        raise ValueError(f"[fail-loud] lots 必须为正，收到 {lots!r}")
    if float(entry_px) <= 0 or float(init_stop) <= 0 or float(r_unit) <= 0:
        raise ValueError("[fail-loud] entry_px/init_stop/r_unit 必须为正")
    if abs(float(tp_r) - 1.5) > 1e-9:
        raise ValueError(f"[fail-loud] 5m 止盈口径固定 1.5R，收到 {tp_r!r}")
    sign = 1.0 if d == "LONG" else -1.0
    tp_px = round(float(entry_px) + sign * 1.5 * float(r_unit), 6)
    # 自洽校验：init_stop 必须与 entry 相距正好 1R（否则 R 口径被污染）
    if abs(abs(float(entry_px) - float(init_stop)) - float(r_unit)) > max(1e-4, r_unit * 1e-4):
        raise ValueError(
            f"[fail-loud] |entry-init_stop|={abs(float(entry_px)-float(init_stop)):.4f} "
            f"≠ r_unit={float(r_unit):.4f}，5m 1R 口径不一致")
    return {
        "signal_id": str(signal_id),
        "open_order_id": open_order_id,
        "symbol_continuous": str(symbol_continuous),
        "real_symbol": str(real_symbol),
        "exchange": str(exchange),
        "direction": d,
        "lots": int(lots),
        "entry_px": float(entry_px),
        "init_stop": float(init_stop),
        "r_unit": float(r_unit),
        "tp_px": tp_px,
        "account": str(account),
        "channel": str(channel),
        "pool_id": pool_id,
        "status": "OPEN",
    }


def to_guard_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """把 5m 计划映射成 exit_guard.evaluate 能消费的 plan dict。

    关键：把 tp1 / time_stop / trail 全部置空，只留 HARD_STOP 与 1.5R TP2，
    使 evaluate 的返回严格是「二选一、全手数」两条路径之一。
    """
    return {
        "direction": plan["direction"],
        "entry_px": float(plan["entry_px"]),
        "init_stop": float(plan["init_stop"]),
        "r_unit": float(plan["r_unit"]),
        "lots_open": int(plan["lots"]),
        "tp1_px": None,
        "tp1_lots": None,
        "tp2_px": float(plan["tp_px"]),
        "tp2_lots": int(plan["lots"]),
        "rest_lots": 0,
        "time_stop_at": None,
        "trail_stop": None,
        "degraded": False,
    }


def decide(plan: dict[str, Any], *, last_price: float, mult: float = 1.0,
           now: datetime | None = None) -> str | None:
    """返回应触发的离场类型 ``HARD_STOP`` / ``TP`` / None（不触发）。

    直接用 exit_guard.evaluate；因 to_guard_plan 已置空 tp1/time_stop/trail，
    只可能返回 HARD_STOP 或 TP2 两类信号。多类同时出现时按 HARD_STOP 优先
    （防守优先，与 exit_guard 的 HARD_STOP first 一致）。
    """
    from app.execution.exit_guard import evaluate

    sigs = evaluate(
        to_guard_plan(plan), last_price=float(last_price), mult=float(mult), now=now)
    kinds = {s.kind for s in sigs}
    if "HARD_STOP" in kinds:
        return "HARD_STOP"
    if "TP2" in kinds:
        return "TP"
    return None


UPSERT_PLAN = text("""
INSERT INTO five_min_exit_plan
    (signal_id, open_order_id, symbol_continuous, real_symbol, exchange, direction,
     lots, entry_px, init_stop, r_unit, tp_px, account, channel, pool_id, status)
VALUES
    (:signal_id, :open_order_id, :symbol_continuous, :real_symbol, :exchange, :direction,
     :lots, :entry_px, :init_stop, :r_unit, :tp_px, :account, :channel, :pool_id, 'OPEN')
ON CONFLICT (signal_id) DO NOTHING
RETURNING id
""")


def persist_plan(session: Session, plan: dict[str, Any]) -> int | None:
    """落计划；同 signal_id 已存在则幂等返回 None（不覆盖、不重复）。"""
    row = session.execute(UPSERT_PLAN, {
        "signal_id": plan["signal_id"], "open_order_id": plan.get("open_order_id"),
        "symbol_continuous": plan["symbol_continuous"], "real_symbol": plan["real_symbol"],
        "exchange": plan["exchange"], "direction": plan["direction"],
        "lots": plan["lots"], "entry_px": plan["entry_px"],
        "init_stop": plan["init_stop"], "r_unit": plan["r_unit"],
        "tp_px": plan["tp_px"], "account": plan["account"], "channel": plan["channel"],
        "pool_id": plan.get("pool_id"),
    }).first()
    return int(row[0]) if row else None


def open_plans(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(text(
        "SELECT * FROM five_min_exit_plan WHERE status='OPEN' ORDER BY id")).mappings().all()
    return [dict(r) for r in rows]


def record_action(session: Session, plan_id: int, kind: str, lots: int,
                  price_ref: float, order_id: int | None, state: str,
                  note: str = "") -> None:
    session.execute(text(
        "INSERT INTO five_min_exit_action "
        "(plan_id, kind, lots, price_ref, order_id, state, note) "
        "VALUES (:p, :k, :l, :px, :oid, :st, :note)"),
        {"p": plan_id, "k": kind, "l": int(lots), "px": round(float(price_ref), 6),
         "oid": order_id, "st": str(state), "note": note})


def close_plan(session: Session, plan_id: int, kind: str, close_order_id: int | None,
               ) -> None:
    session.execute(text(
        "UPDATE five_min_exit_plan SET status='CLOSED', close_order_id=:oid, "
        "close_kind=:k, closed_at=now(), updated_at=now() "
        "WHERE id=:i AND status='OPEN'"),
        {"i": plan_id, "k": kind, "oid": close_order_id})


__all__ = [
    "build_exit_plan", "to_guard_plan", "decide", "persist_plan",
    "open_plans", "record_action", "close_plan",
]
