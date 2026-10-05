# -*- coding: utf-8 -*-
"""P0-2 Sprint 1 · 网关客户端（HTTP，三态返回）。

**超时 ≠ 失败**（真实资金事故高发点）：网络超时只说明「没收到回执」，不代表交易所
没收到。因此超时返回 ``TIMEOUT_UNKNOWN``，调用方**必须先 query_order 查单判定**，
**禁止直接重试 submit**（会重复下单）。

三态：
  * ``ACK``             —— 已受理，拿到 broker_order_id
  * ``REJECT``          —— 明确被拒（带 error）
  * ``TIMEOUT_UNKNOWN`` —— 超时，**须查单**再决定
"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx

#: 连接/读取超时分离：连接快失败，读取给足（网关可能慢）。
#: ⚠ httpx.Timeout 必须给全四个参数或含 default，否则构造即 ValueError（实测踩过）。
TIMEOUT = httpx.Timeout(connect=3.0, read=8.0, write=8.0, pool=8.0)


class BrokerUnavailable(RuntimeError):
    """网关不可达（连接阶段失败，可重试）。"""


def _base_url() -> str:
    url = os.getenv("BROKER_HTTP_BASE_URL", "").rstrip("/")
    if not url:
        raise BrokerUnavailable(
            "[fail-loud] 未配置 BROKER_HTTP_BASE_URL，拒绝用默认地址下单（真实资金风险）"
        )
    return url


def submit(payload: dict[str, Any], *, retries: int = 0) -> dict[str, Any]:
    """提交订单。默认**不重试**（重试须由调用方在查单确认后显式发起）。"""
    base = _base_url()
    attempt = 0
    while True:
        try:
            r = httpx.post(f"{base}/order", json=payload, timeout=TIMEOUT)
        except httpx.TimeoutException:
            return {"state": "TIMEOUT_UNKNOWN",
                    "detail": "请求超时，未收到回执；须 query_order 查单，禁止直接重试"}
        except httpx.HTTPError as e:
            attempt += 1
            if attempt > retries:
                raise BrokerUnavailable(f"[fail-loud] 网关不可达：{e!r}") from e
            time.sleep(1.0 * attempt)
            continue
        if r.status_code >= 400:
            return {"state": "REJECT", "error": r.text, "http_status": r.status_code}
        try:
            body = r.json()
        except ValueError:
            # 回执非 JSON：无法判定是否受理，按未知处理（须查单），不猜
            return {"state": "TIMEOUT_UNKNOWN", "detail": f"回执非 JSON：{r.text[:200]!r}"}
        return {"state": "ACK", "broker_order_id": body.get("order_id"), "raw": body}


def query_order(broker_order_id: str) -> dict[str, Any]:
    """按 broker_order_id 查单（超时判定后的唯一澄清手段）。"""
    base = _base_url()
    r = httpx.get(f"{base}/order/{broker_order_id}", timeout=TIMEOUT)
    if r.status_code == 404:
        return {"state": "NOT_FOUND"}
    if r.status_code >= 400:
        return {"state": "REJECT", "error": r.text, "http_status": r.status_code}
    return {"state": "OK", **r.json()}


def cancel(broker_order_id: str) -> dict[str, Any]:
    """撤单。"""
    base = _base_url()
    try:
        r = httpx.post(f"{base}/order/{broker_order_id}/cancel", timeout=TIMEOUT)
    except httpx.TimeoutException:
        return {"state": "TIMEOUT_UNKNOWN", "detail": "撤单超时，须查单确认是否已撤"}
    if r.status_code >= 400:
        return {"state": "REJECT", "error": r.text, "http_status": r.status_code}
    return {"state": "OK", **r.json()}


def probe(channel: str = "SIM") -> dict[str, Any]:
    """健康探测（monitor.py 用）：返回 healthy/latency_ms/detail。"""
    base = _base_url()
    t0 = time.perf_counter()
    try:
        r = httpx.get(f"{base}/health", timeout=TIMEOUT)
        latency = int((time.perf_counter() - t0) * 1000)
        return {"channel": channel, "healthy": r.status_code < 400, "latency_ms": latency,
                "detail": f"HTTP {r.status_code}"}
    except httpx.HTTPError as e:
        latency = int((time.perf_counter() - t0) * 1000)
        return {"channel": channel, "healthy": False, "latency_ms": latency,
                "detail": f"{type(e).__name__}: {e!r}"}
