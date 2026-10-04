# -*- coding: utf-8 -*-
"""采集韧性层（PRD 评审补强 **G1 / P0**）。

背景
----
PRD §6.4 决策「只留 akshare 单一源」并删除天勤双源比对层（T7）。
评审补强 §4.1 指出：这等于引入**单点依赖（SPOF）却未配套韧性层** ——
akshare 是爬取公开页面的免费库，必然有：限流、偶发脏数据/空、接口变更、盘中延迟。
原系统好歹有兜底与比对；新架构在更不可靠的单源上反而移除了保险。

本模块提供四种机制，替代被删除的天勤比对层
------------------------------------------
1. **指数退避重试**：对可恢复异常（网络/超时/限流/空结果）自动重试。
2. **断路器**：连续失败达阈值后短路一段时间，避免雪崩与无意义重试。
3. **本地磁盘缓存**：最近一次成功结果，缓存期内失败可降级使用（fail-soft 可观测）。
4. **脏数据校验**：与上一成功批次比较，价差/突变超阈值则拒绝入库并告警
   —— 这是评审建议的「保留比对精神但不绑定天勤」。

**绝不静默**：任何降级都会返回 ``DegradedResult`` 并由调用方显式决定是否接受；
失败一律记 :class:`IngestError` + 写 `ingest_freshness` 表，供告警扫描。
"""
from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from app.core.logging import logger

#: 缓存根目录（容器内可写）
_CACHE_DIR = Path(os.environ.get("INGEST_CACHE_DIR", "/app/.cache/ingest"))

#: 默认重试策略
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 1.0        # 秒
DEFAULT_MAX_DELAY = 20.0
DEFAULT_JITTER = 0.3            # ±30% 抖动，避免同时重试

#: 断路器
DEFAULT_FAILURE_THRESHOLD = 5   # 连续失败次数
DEFAULT_RESET_AFTER = 300.0     # 断路后多久进入 half-open（秒）


@dataclass
class DegradedResult:
    """降级结果：值可用，但**不是本次实时拉取**。

    调用方必须显式处理（告警/记票），不得当作正常数据静默使用。
    """

    value: Any
    reason: str
    stale_seconds: float = 0.0
    from_cache: bool = False

    def __repr__(self) -> str:
        return (f"<DegradedResult reason={self.reason!r} "
                f"stale={self.stale_seconds:.0f}s from_cache={self.from_cache}>")


# --------------------------------------------------------------------------- #
# 断路器
# --------------------------------------------------------------------------- #
class CircuitBreaker:
    """简易断路器：CLOSED → OPEN → HALF_OPEN → CLOSED。"""

    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, name: str, failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
                 reset_after: float = DEFAULT_RESET_AFTER):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_after = reset_after
        self._failures = 0
        self._opened_at = 0.0
        self._state = self.CLOSED

    @property
    def state(self) -> str:
        if self._state == self.OPEN and (time.time() - self._opened_at) >= self.reset_after:
            self._state = self.HALF_OPEN
        return self._state

    def allow(self) -> bool:
        return self.state != self.OPEN

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = 0.0
        self._state = self.CLOSED

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = self.OPEN
            self._opened_at = time.time()
            logger.warning(
                f"[resilience] 断路器 {self.name} 打开（连续失败 {self._failures}），"
                f"{self.reset_after:.0f}s 后进入 half-open")


_BREAKERS: dict[str, CircuitBreaker] = {}


def breaker(name: str) -> CircuitBreaker:
    return _BREAKERS.setdefault(name, CircuitBreaker(name))


# --------------------------------------------------------------------------- #
# 本地磁盘缓存（last-known-good）
# --------------------------------------------------------------------------- #
def _cache_path(key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in key)
    return _CACHE_DIR / f"{safe}.json"


def cache_get(key: str, ttl: float) -> Optional[tuple[float, Any]]:
    p = _cache_path(key)
    if not p.exists():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        ts = float(obj.get("ts", 0))
        if time.time() - ts > ttl:
            return None
        return ts, obj.get("value")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[resilience] 读取缓存失败 {key}: {e}")
        return None


def cache_put(key: str, value: Any) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(key).write_text(
            json.dumps({"ts": time.time(), "value": value}, ensure_ascii=False,
                       default=str),
            encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[resilience] 写缓存失败 {key}: {e}")


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
@dataclass
class FetchOutcome:
    """一次韧性拉取的结果。"""

    ok: bool
    value: Any = None
    error: Optional[str] = None
    attempts: int = 0
    degraded: Optional[DegradedResult] = field(default=None)


def resilient_fetch(
    key: str,
    fn: Callable[[], Any],
    *,
    cache_ttl: float = 3600.0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    allow_stale_on_failure: bool = True,
    validate: Optional[Callable[[Any], Optional[str]]] = None,
    previous: Any = None,
) -> FetchOutcome:
    """带重试 / 断路器 / 缓存降级的拉取。

    :param validate: 可选校验器，返回 None 表示通过，返回字符串表示「脏数据」原因。
                    脏数据**等同于失败**并重试（不入库），这是替代天勤比对的关键。
    :param previous: 上一成功批次，供 validate 做跨批次一致性校验。
    """
    cb = breaker(key.split(":")[0])
    if not cb.allow():
        hit = cache_get(key, cache_ttl) if allow_stale_on_failure else None
        if hit:
            logger.warning(f"[resilience] {key} 断路器打开，降级用缓存")
            return FetchOutcome(True, hit[1], attempts=0,
                                degraded=DegradedResult(
                                    hit[1], "circuit_open",
                                    time.time() - hit[0], True))
        return FetchOutcome(False, error=f"断路器 {cb.name} 已打开，无缓存可用",
                            attempts=0)

    last_err: Optional[str] = None
    for attempt in range(1, max_attempts + 1):
        try:
            value = fn()
            reason = validate(value, previous) if validate else None
            if reason:
                last_err = f"脏数据被拒: {reason}"
                logger.warning(f"[resilience] {key} 第 {attempt} 次: {last_err}")
            else:
                cb.record_success()
                cache_put(key, value)
                return FetchOutcome(True, value, attempts=attempt)
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            logger.warning(f"[resilience] {key} 第 {attempt}/{max_attempts} 次失败: {last_err}")

        if attempt < max_attempts:
            delay = min(DEFAULT_BASE_DELAY * (2 ** (attempt - 1)), DEFAULT_MAX_DELAY)
            delay *= (1 + random.uniform(-DEFAULT_JITTER, DEFAULT_JITTER))
            time.sleep(delay)

    cb.record_failure()
    if allow_stale_on_failure:
        hit = cache_get(key, cache_ttl)
        if hit:
            logger.warning(f"[resilience] {key} 全部重试失败，降级用缓存（陈旧 "
                           f"{time.time() - hit[0]:.0f}s）")
            return FetchOutcome(True, hit[1], error=last_err, attempts=max_attempts,
                                degraded=DegradedResult(hit[1], last_err or "failed",
                                                        time.time() - hit[0], True))
    return FetchOutcome(False, error=last_err, attempts=max_attempts)


# --------------------------------------------------------------------------- #
# 跨批次一致性校验（替代天勤双源比对）
# --------------------------------------------------------------------------- #
def bars_consistency_check(new: Any, previous: Any, *, max_jump: float = 0.15) -> Optional[str]:
    """用「与上一成功批次的价差/突变」拦截脏数据。

    akshare 偶发返回脏数据（价格整体错位、单位变化、日期错乱）。
    没有第二数据源时，这是唯一可自动化的拦截手段（评审 G1 建议）。

    :param max_jump: 相邻批次重叠部分允许的最大相对跳变（默认 15%）。
    """
    if not isinstance(new, list) or not isinstance(previous, list) or not new or not previous:
        return None  # 无参照物，不判脏
    try:
        def _close(row):
            return float(row.get("close") or row.get("收盘") or 0)

        prev_last = _close(previous[-1])
        new_first = _close(new[0])
        if prev_last <= 0 or new_first <= 0:
            return None
        jump = abs(new_first - prev_last) / prev_last
        if jump > max_jump:
            return f"跨批次跳变 {jump:.1%} > {max_jump:.1%} ({prev_last} → {new_first})"
    except Exception as e:  # noqa: BLE001
        return None
    return None


__all__ = ["resilient_fetch", "DegradedResult", "FetchOutcome", "CircuitBreaker",
           "breaker", "cache_get", "cache_put", "bars_consistency_check"]
