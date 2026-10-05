# -*- coding: utf-8 -*-
"""P0-2 Sprint 1 · 通道健康巡检（写入 execution_channel_health，供告警用）。

要点
----
* 每次探测都**落库**（含 healthy/latency_ms/detail），便于事后判断"当时网关是否可达"。
* 探测异常**不抛异常**（巡检不该把调用方打挂），但会把 detail 记全并 healthy=False——
  即"失败要留痕、但不阻断主流程"。
* 同时暴露 :func:`health_snapshot` 供 API/告警读取当前最新状态。
"""
from __future__ import annotations

from typing import Any

from app.core.logging import logger
from app.execution import persistence
from app.execution.broker import describe, get_broker


def probe_once(channel: str = "SIM") -> dict[str, Any]:
    """探测一次网关健康并落库，返回探测结果。"""
    try:
        result = get_broker().probe(channel)
    except Exception as e:  # noqa: BLE001 —— 巡检不因探测失败而抛出
        result = {"channel": channel, "healthy": False, "latency_ms": None,
                  "detail": f"{type(e).__name__}: {e!r}"}
    try:
        persistence.record_health(
            result.get("channel", channel),
            healthy=bool(result.get("healthy")),
            latency_ms=result.get("latency_ms"),
            detail=result.get("detail"),
        )
    except Exception as e:  # noqa: BLE001 —— 落库失败也不打断巡检
        logger.warning(f"[monitor] 通道健康落库失败：{e!r}")
    return result


def health_snapshot(channel: str = "SIM") -> dict[str, Any]:
    """返回当前网关描述 + 最新一次探测结论（供 /execution/health）。"""
    d = describe()
    try:
        latest = probe_once(d.get("channel") or channel)
    except Exception as e:  # noqa: BLE001
        latest = {"healthy": False, "detail": f"{type(e).__name__}: {e!r}"}
    return {**d, "latest": latest}

