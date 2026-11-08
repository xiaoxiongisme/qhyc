# -*- coding: utf-8 -*-
"""实盘持仓同步 —— **唯一**写 ``execution_position`` 的业务入口（默认关闭）。

一句话说清它现在的状态
======================
**关闭（``position_sync_enabled=false``）。** 关闭时本模块的每个写路径都在
第一步返回 ``applied=False``，**不碰任何持仓表、不读也不写**。

为什么会有这个模块
==================
2026-11-08 现状核查（CL-20261108-26）得到一个不太好听的结论：**「持仓同步」在
代码里根本不存在，只留下一条断链 + 一句谎话。**

* ``execution_position`` 只能由 :func:`app.execution.persistence.upsert_position` 写；
* 它唯一的业务调用方 :func:`app.execution.position_manager.apply_fill` **零调用方**；
* ``POST /execution/bridge/report`` 只写 ``execution_fill``，从不回写持仓，
  但它的 docstring 写着「持仓由 ``upsert_position`` 维护」——代码没做过这件事。

后果不是「没有持仓管理」这么温和，而是：桥上报了成交、台账停在旧值，
而所有依赖 ``execution_position`` 的下游（开平推导 G1、止损成本价 G3）都会
**读到过期事实且不报错**。没有比「看起来能用、其实用的是旧数」更难发现的了。

用户 2026-11-08 拍板：**回写实际持仓暂时不做**，但半成品不许留着——
它比「没有」更危险。于是有两个入口（成交回报 / 手工快照）被收进本模块，
用一个开关一起关掉，后期完善时只改这一处。

两路入口
========
1. :func:`sync_from_fill` —— 桥回报成交（``PARTIAL``/``FILLED``）后滚动持仓。
   语义是**增量**：在现持仓基础上按开/平方向加减。
2. :func:`sync_from_snapshot` —— 用户从交易软件抄下**实际持仓**后一次性覆盖。
   语义是**绝对值**：直接把 ``net/long/short/today`` 设成你看到的数。
   这条路径存在的理由：柜台真值永远比我们的推导权威。当两者不一致时，
   正确的做法不是去修推导，而是承认柜台对、把台账对齐过去。

为什么用 ``cfg_feature_switch`` 而不是配置文件
=============================================
开关要能「改一行 SQL 就停」，不能要求改 yaml + 重启（``config/cloud.yaml``
还被 ``.gitignore`` 排除，改它连 git 都看不见）。所以走 :mod:`app.core.feature_switch`，
带 60s TTL 缓存，异常一律回退 ``default=False``——**失败方向必须朝「不同步」**，
绝不能因为读不到开关就自作主张开始写持仓。

开启前必须满足的条件（写在这里，防止下次又被当成「打开就行」）
==============================================================
1. ``bridge_report`` 的 ``filled_lots`` / ``avg_price`` 在真实成交中确实非空且准确
   （当前 ``ReportReq`` 默认 0 / 0.0，未填就是「成交了但不知道成交多少」）；
2. 至少连续 5 个交易日「台账 vs 柜台」逐日对账一致，且差异全部有解释；
3. ``execution_position`` 的 ``channel`` / ``account`` 口径与无限易实际账户一一对应；
4. 有回滚路径：``position_sync_enabled=false`` 能立刻回到「只记成交、不动台账」。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.feature_switch import is_enabled
from app.core.logging import logger

__all__ = [
    "SWITCH_KEY",
    "SyncResult",
    "is_sync_enabled",
    "sync_from_fill",
    "sync_from_snapshot",
    "status",
]

#: 总开关键（migrations/060 建，默认 false）
SWITCH_KEY = "position_sync_enabled"


@dataclass(frozen=True)
class SyncResult:
    """一次同步尝试的结果。

    ``applied=False`` 时 ``reason`` 必有值，且调用方**不得**把它当成成功。
    这样「关掉开关」与「同步失败」在返回值上是可区分的两种状态，
    不会像历史上那样混成一句 ``ok=True``。
    """

    applied: bool
    reason: str = ""
    position: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"applied": self.applied, "reason": self.reason}
        if self.position is not None:
            out["position"] = self.position
        return out


def is_sync_enabled() -> bool:
    """开关是否开启。读不到一律当**关闭**（fail-closed）。"""
    try:
        return bool(is_enabled(SWITCH_KEY, default=False))
    except Exception as exc:                                  # noqa: BLE001
        logger.opt(exception=True).warning(
            f"[position_sync] 读开关异常，按关闭处理：{exc}")
        return False


def _blocked(op: str) -> SyncResult:
    """开关关闭时的统一出口（唯一挡板，避免各路径各写一遍判断）。"""
    reason = f"position_sync_disabled：开关 {SWITCH_KEY}=false，{op} 未执行"
    logger.debug(f"[position_sync] {reason}")
    return SyncResult(applied=False, reason=reason)


def sync_from_fill(
    *,
    real_symbol: str,
    action: str,
    direction: str,
    fill_lots: int,
    fill_price: float | None = None,
    channel: str = "SIM",
    account: str | None = None,
) -> SyncResult:
    """成交回报 → 滚动 ``execution_position``（**增量**语义）。

    Args:
        action: ``OPEN`` / ``CLOSE`` / ``CLOSE_TODAY`` / ``CLOSE_YEST``
                （由 :func:`app.execution.position_manager.derive_action` 推导，
                不接受调用方拿 ``direction`` 自己猜——那正是 G1 要防的事故）。
        direction: ``BUY`` / ``SELL``。
        fill_lots: 本次**实际成交**手数；≤0 视为无效回报，不写。
        fill_price: 成交均价；``None`` 表示未知，此时不更新 ``avg_open_price``
                    （G3 读不到成本价要 fail-loud，不能让这里猜一个）。
    """
    if not is_sync_enabled():
        return _blocked("成交回报回写持仓")

    if fill_lots is None or int(fill_lots) <= 0:
        logger.warning(
            f"[position_sync] {real_symbol} 成交手数 {fill_lots!r} 无效 → 不回写持仓"
            "（宁可台账不动，也不能按 0 手记一笔成交）")
        return SyncResult(applied=False, reason=f"invalid_fill_lots={fill_lots!r}")

    d = (direction or "").upper()
    a = (action or "").upper()
    if d not in ("BUY", "SELL"):
        logger.error(f"[position_sync] {real_symbol} direction={direction!r} 非法 → 拒绝回写")
        return SyncResult(applied=False, reason=f"invalid_direction={direction!r}")

    from app.execution import position_manager

    try:
        pos = position_manager.apply_fill(
            real_symbol=real_symbol,
            action=a,
            direction=d,
            fill_lots=int(fill_lots),
            fill_price=fill_price,
            channel=channel,
            account=account,
        )
    except ValueError as exc:
        # apply_fill 对未知 action 显式抛错；这里转成结果对象，不吞成成功
        logger.error(f"[position_sync] {real_symbol} 回写持仓被拒：{exc}")
        return SyncResult(applied=False, reason=f"rejected: {exc}")

    logger.info(
        f"[position_sync] {real_symbol} {a} {d} {fill_lots} 手 @ {fill_price} → "
        f"net={pos.net_lots} long={pos.long_lots} short={pos.short_lots} "
        f"today={pos.today_lots}")
    return SyncResult(applied=True, reason="ok", position={
        "net_lots": pos.net_lots,
        "long_lots": pos.long_lots,
        "short_lots": pos.short_lots,
        "today_lots": pos.today_lots,
    })


def sync_from_snapshot(
    *,
    real_symbol: str,
    net_lots: int,
    long_lots: int | None = None,
    short_lots: int | None = None,
    today_lots: int = 0,
    avg_open_price: float | None = None,
    channel: str = "SIM",
    account: str | None = None,
) -> SyncResult:
    """手工回写**实际持仓**（**绝对值**语义，覆盖而非累加）。

    用于「我在交易软件里看到的就是这些，按这个对齐台账」。缺省时
    ``long_lots`` / ``short_lots`` 由 ``net_lots`` 的符号推出（只知净仓的常见场景）。

    ⚠ 这是**破坏性覆盖**：它会把之前所有增量推导的结果直接替换掉。所以：
      * 开关关闭时同样一步就返回，不做任何校验之外的副作用；
      * 入参自相矛盾（如 net=+2 却 long=0/short=0）直接拒绝，不做「尽力修正」。
    """
    if not is_sync_enabled():
        return _blocked("手工回写实际持仓")

    net = int(net_lots)
    lng = int(long_lots) if long_lots is not None else max(net, 0)
    shrt = int(short_lots) if short_lots is not None else max(-net, 0)
    if net != lng - shrt:
        logger.error(
            f"[position_sync] {real_symbol} 快照自相矛盾：net={net} 但 long-short="
            f"{lng - shrt} → 拒绝写入（不猜哪个数是对的）")
        return SyncResult(applied=False,
                          reason=f"inconsistent_snapshot net={net} long={lng} short={shrt}")
    if lng < 0 or shrt < 0 or int(today_lots) < 0:
        logger.error(f"[position_sync] {real_symbol} 快照出现负手数 → 拒绝写入")
        return SyncResult(applied=False, reason="negative_lots")

    from app.execution import persistence

    persistence.upsert_position(
        real_symbol=real_symbol,
        net_lots=net,
        long_lots=lng,
        short_lots=shrt,
        today_lots=int(today_lots),
        avg_open_price=avg_open_price,
        channel=channel,
        account=account,
    )
    logger.warning(
        f"[position_sync] {real_symbol} 持仓被**手工快照覆盖** → net={net} "
        f"long={lng} short={shrt} today={today_lots} avg={avg_open_price}"
        f"（channel={channel} account={account}）")
    return SyncResult(applied=True, reason="ok", position={
        "net_lots": net, "long_lots": lng, "short_lots": shrt,
        "today_lots": int(today_lots),
    })


def status() -> dict[str, Any]:
    """当前同步状态（供 ``/execution/bridge/health`` 与运维核对）。"""
    on = is_sync_enabled()
    return {
        "enabled": on,
        "switch_key": SWITCH_KEY,
        "entrypoint": "app.execution.position_sync",
        "note": ("持仓同步已开启：成交回报会滚动 execution_position"
                 if on else
                 "持仓同步已关闭（当前口径）：成交只记 execution_fill，不碰任何持仓表"),
    }
