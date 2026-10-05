# -*- coding: utf-8 -*-
"""P0-2 · 网关门面（按配置选择仿真 / HTTP 网关）。

上层（execution_runtime / monitor）只依赖本模块的**三态接口**，
不直接 import 具体网关——换网关（sim ↔ ctp/qmt）不改上层一行业务逻辑。

选择规则（app/core/config.ExecutionConfig）：
  * ``EXECUTION_BROKER=sim``（**默认**）→ :mod:`app.execution.sim_broker`（零资金）
  * ``ctp`` / ``qmt`` / ``http``        → :mod:`app.execution.broker_http`（需真实网关）

fail-loud：非 sim 且缺 base_url 时，:mod:`broker_http` 会在下单时抛错而非用默认地址。
"""
from __future__ import annotations

import os
from types import ModuleType

from app.core.logging import logger

SIM_MODES = ("sim", "simnow", "paper")


def get_broker() -> ModuleType:
    """返回当前生效的网关模块（sim_broker 或 broker_http）。"""
    mode = (os.getenv("EXECUTION_BROKER") or "sim").strip().lower()
    if mode in SIM_MODES:
        from app.execution import sim_broker

        return sim_broker
    if mode in ("ctp", "qmt", "http"):
        from app.execution import broker_http

        return broker_http
    # 未知值不静默当 sim（那是"看起来在跑其实没下单"的经典坑）
    raise ValueError(
        f"[fail-loud] 未知的 EXECUTION_BROKER={mode!r}；"
        f"合法值：sim（仿真，默认）/ ctp / qmt / http"
    )


def broker_mode() -> str:
    """当前网关模式标识（供 API/health 展示，避免"以为在实盘其实在仿真"）。"""
    return (os.getenv("EXECUTION_BROKER") or "sim").strip().lower()


def describe() -> dict:
    """网关描述（供 /execution/health 暴露，明确当前是否真实资金通道）。"""
    mode = broker_mode()
    return {
        "mode": mode,
        "is_simulation": mode in SIM_MODES,
        "base_url": (os.getenv("EXECUTION_BROKER_BASE_URL") or "") if mode not in SIM_MODES else "",
        "account": os.getenv("EXECUTION_ACCOUNT", ""),
        "note": "仿真通道，零真实资金" if mode in SIM_MODES else "真实网关通道",
    }


def log_mode_once() -> None:
    """启动时打印网关模式（fail-loud：避免误把仿真当实盘）。"""
    d = describe()
    logger.info(
        f"[exec] 网关模式={d['mode']}（{'仿真·零资金' if d['is_simulation'] else '真实资金'}）"
        + (f" base_url={d['base_url']}" if d["base_url"] else "")
    )

