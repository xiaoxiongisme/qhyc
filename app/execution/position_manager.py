# -*- coding: utf-8 -*-
"""P0-2 Sprint 1 · 持仓聚合与开平推导（G1/G2）。

G1（开平标志）：**必须**由「目标方向 + 当前持仓」推导，禁止用 direction 单独推断——
    这是 P0-2 三个硬缺口之一。开错方向会直接变成反向开仓（真实资金事故）。

G2（持仓真源）：持仓以本模块聚合结果为准（DDL 030 `execution_position` 四字段），
    不依赖方向字符串猜测。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.execution import persistence

VALID_EXCHANGES = ("SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX")


@dataclass(frozen=True)
class Position:
    """某 (real_symbol, channel) 的持仓快照。"""

    net_lots: int = 0        # 净持仓，正=多 负=空
    long_lots: int = 0
    short_lots: int = 0
    today_lots: int = 0      # 今仓（平今/平昨费率与保证金差异必需）

    @property
    def is_flat(self) -> bool:
        return self.net_lots == 0


def load_position(real_symbol: str, *, channel: str = "SIM",
                  account: str | None = None) -> Position:
    """从 execution_position 读当日持仓；**无记录即视为空仓**（新品种首次开仓场景）。"""
    row = persistence.get_position(real_symbol, channel=channel, account=account)
    if row is None:
        return Position()
    return Position(
        net_lots=int(row["net_lots"]),
        long_lots=int(row["long_lots"]),
        short_lots=int(row["short_lots"]),
        today_lots=int(row["today_lots"]),
    )


def derive_action(*, direction: str, pos: Position, exchange: str) -> str:
    """由「目标方向 + 当前持仓」推导开平标志。

    规则：
      * 空仓 / 同向加仓 → ``OPEN``
      * 反向平仓：优先平今 (``CLOSE_TODAY``)，无今仓则平昨 (``CLOSE_YEST``)
      * **CZCE 无平今指令** → 一律 ``CLOSE``（交易所差异在此收敛）
    """
    if direction not in ("BUY", "SELL"):
        raise ValueError(f"[fail-loud] direction 非法 {direction!r}，仅 BUY/SELL")
    if exchange not in VALID_EXCHANGES:
        raise ValueError(
            f"[fail-loud] exchange 未知 {exchange!r}，无法推导开平（不默认按 SHFE 处理）"
        )

    if pos.is_flat:
        return "OPEN"
    same_dir = (direction == "BUY" and pos.net_lots > 0) or (
        direction == "SELL" and pos.net_lots < 0
    )
    if same_dir:
        return "OPEN"

    if exchange == "CZCE":
        return "CLOSE"          # 郑商所无平今指令，差异在此收敛
    return "CLOSE_TODAY" if pos.today_lots > 0 else "CLOSE_YEST"


def apply_fill(*, real_symbol: str, action: str, direction: str, fill_lots: int,
               channel: str = "SIM", account: str | None = None) -> Position:
    """成交后滚动持仓（写回 execution_position），返回新快照。"""
    pos = load_position(real_symbol, channel=channel, account=account)
    sign = 1 if direction == "BUY" else -1
    net, lng, shrt, today = pos.net_lots, pos.long_lots, pos.short_lots, pos.today_lots

    if action == "OPEN":
        net += sign * fill_lots
        if sign > 0:
            lng += fill_lots
        else:
            shrt += fill_lots
    elif action in ("CLOSE", "CLOSE_TODAY", "CLOSE_YEST"):
        net -= sign * fill_lots
        if sign > 0:      # 买入平空
            shrt = max(0, shrt - fill_lots)
        else:             # 卖出平多
            lng = max(0, lng - fill_lots)
        if action == "CLOSE_TODAY":
            today = max(0, today - fill_lots)
        elif action == "CLOSE":   # CZCE 不区分今昨，按今仓优先扣（best-effort）
            today = max(0, today - min(today, fill_lots))
        # CLOSE_YEST：不动 today
    else:
        raise ValueError(f"[fail-loud] 未知 action {action!r}")

    persistence.upsert_position(
        real_symbol=real_symbol, net_lots=net, long_lots=lng, short_lots=shrt,
        today_lots=today, channel=channel, account=account,
    )
    return Position(net_lots=net, long_lots=lng, short_lots=shrt, today_lots=today)
