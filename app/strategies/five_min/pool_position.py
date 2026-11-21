# -*- coding: utf-8 -*-
"""5M-P0C · 池级单持仓闸（CL-20261121-06）。

为什么不读 execution_position
----------------------------
``position_sync_enabled=false``（CL-20261108-26 用户拍板），``execution_position``
不是柜台权威；拿它做「在场」判断会用过期事实误判，可能重复开仓。
本模块改以**订单账本**为重建源：同一 ``pool_id`` 下按时间顺序回放 OPEN / CLOSE
订单，只认非拒单/撤单/错误单，得出「当前是否在场、持有哪个合约」。
数据库是唯一真相，内存只是缓存；进程重启、多 worker 都能重建。

默认兼容
--------
* 独立开关 ``five_min_pool_position_enabled``（迁移 083，默认 false）；
* 请求不带 ``pool_id`` 时闸完全不生效；
* 不写 ``execution_position`` / ``fusion_position``，不碰 CL-20261119-01 的
  BT-POOL 动态豁免逻辑，也不触发 POSITION-SYNC-SINGLE-ENTRY 的 AST 守卫。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from app.core.feature_switch import is_enabled

SWITCH_KEY = "five_min_pool_position_enabled"
_ACTIVE_ORDER_STATUS = {"NEW", "SENT", "PARTIAL", "FILLED"}
_DEAD_ORDER_STATUS = {"CANCELED", "REJECTED", "ERROR"}


@dataclass(frozen=True)
class PoolSlot:
    pool_id: str
    real_symbol: str
    signal_id: str | None
    order_id: int | None
    as_of: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "pool_id": self.pool_id,
            "real_symbol": self.real_symbol,
            "signal_id": self.signal_id,
            "order_id": self.order_id,
            "as_of": self.as_of,
        }


class PoolPositionGuard:
    """同一池最多一笔在场单；平仓单只放行给当前持仓合约。"""

    def __init__(self, enabled: bool | None = None) -> None:
        self.enabled = is_enabled(SWITCH_KEY, default=False) if enabled is None else bool(enabled)
        self._slots: dict[str, PoolSlot] = {}

    # ------------------------------------------------------------ 重建
    def rebuild(self, pool_id: str, rows: Iterable[dict[str, Any]]) -> PoolSlot | None:
        """从订单账本回放一个池的当前在场状态（纯函数式，便于离线测试）。"""
        events = sorted(
            (r for r in rows if r.get("pool_id") == pool_id
             and r.get("status") not in _DEAD_ORDER_STATUS),
            key=lambda r: (r.get("created_at") or datetime.min, int(r.get("id") or 0)),
        )
        slot: PoolSlot | None = None
        for r in events:
            action = str(r.get("action") or "").upper()
            if action == "OPEN" and slot is None:
                slot = PoolSlot(pool_id=pool_id, real_symbol=str(r["real_symbol"]),
                                signal_id=r.get("signal_id"), order_id=r.get("id"),
                                as_of=r.get("created_at"))
            elif action in ("CLOSE", "CLOSE_TODAY", "CLOSE_YEST"):
                slot = None
        if slot is None:
            self._slots.pop(pool_id, None)
        else:
            self._slots[pool_id] = slot
        return slot

    def sync_from_db(self, pool_id: str) -> PoolSlot | None:
        from app.execution.persistence import list_orders_by_pool

        return self.rebuild(pool_id, list_orders_by_pool(pool_id))

    # ------------------------------------------------------------ 门禁
    def check_open(
        self, pool_id: str | None, real_symbol: str,
        signal_id: str | None = None, *, sync: bool = True,
    ) -> tuple[bool, str]:
        if not self.enabled or not pool_id:
            return True, "池级闸未启用（开关关闭或未声明 pool_id）"
        slot = self.sync_from_db(pool_id) if sync else self._slots.get(pool_id)
        if slot is not None:
            return False, (
                f"池 {pool_id} 已有在场单 {slot.real_symbol}(order={slot.order_id})，"
                f"拒绝新开仓 {real_symbol}；先平掉原仓再接新信号"
            )
        return True, "池内无在场单，允许开仓"

    def check_close(
        self, pool_id: str | None, real_symbol: str, *, sync: bool = True,
    ) -> tuple[bool, str]:
        """平仓闸：池内无仓或正是本合约才放行，防错平别的池成员。"""
        if not self.enabled or not pool_id:
            return True, "池级闸未启用"
        slot = self.sync_from_db(pool_id) if sync else self._slots.get(pool_id)
        if slot is None:
            return True, "池内无在场单，平仓单放行（幂等/仓位真源仍另行校验）"
        if slot.real_symbol != real_symbol:
            return False, (
                f"池 {pool_id} 在场合约为 {slot.real_symbol}，"
                f"拒绝平仓 {real_symbol}（跨合约平仓会打开反向裸仓）")
        return True, "平本池持仓，放行"

    def mark_open(self, pool_id: str | None, real_symbol: str,
                  signal_id: str | None = None, order_id: int | None = None) -> None:
        if self.enabled and pool_id:
            self._slots[pool_id] = PoolSlot(pool_id, real_symbol, signal_id, order_id,
                                            datetime.now())

    def mark_close(self, pool_id: str | None, real_symbol: str | None = None) -> None:
        if self.enabled and pool_id:
            cur = self._slots.get(pool_id)
            if cur is None or real_symbol is None or cur.real_symbol == real_symbol:
                self._slots.pop(pool_id, None)

    def slot(self, pool_id: str) -> PoolSlot | None:
        return self._slots.get(pool_id)

    def reset(self) -> None:
        self._slots.clear()
