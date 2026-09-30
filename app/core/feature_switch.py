# -*- coding: utf-8 -*-
"""功能开关加载器（数据库控制，替代 env / 代码常量）。

设计意图（用户 2026-09-30 要求）
------------------------------
原本散落在 env 变量 / 代码常量里的功能开关（DATA_SELFCHECK_ENABLED /
PORTFOLIO_BRAKE_ENABLED / FACTOR_* / CLOUD_SYNC_ENABLED / rebuild_fut_kline
等）统一收口到 `cfg_feature_switch` 表。运维改一行 UPDATE 即启停，
**不必改代码、不必改 env、不必重启冷路径**。

安全约定
--------
* 任何异常（表不存在 / 连接失败 / 迁移未执行）都**回退到调用方提供的 default**，
  绝不抛错阻断业务（熔断失效可以，误杀不行）。
* 进程内 60s TTL 缓存，避免热路径每调用都打库；运维改开关后最多 60s 生效。
* 本模块是「只读查询」，不负责写表（写表由迁移 008 / 运维 SQL 完成）。

用法
----
    from app.core.feature_switch import is_enabled, get_value, list_switches
    if is_enabled("portfolio_brake_enabled"): ...
    lookback = int(get_value("cloud_sync_enabled", "30") or "30")
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from sqlalchemy import text

from app.core.db import get_engine

logger = logging.getLogger(__name__)

_TTL = 60.0  # 秒
_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, loader) -> object:
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and (now - hit[0]) < _TTL:
        return hit[1]
    val = loader()
    _cache[key] = (now, val)
    return val


def _query_one(sql: str, params: dict) -> Optional[tuple]:
    try:
        with get_engine().connect() as conn:
            return conn.execute(text(sql), params).first()
    except Exception as e:  # noqa: BLE001  表不存在/连接失败 → 回退 default
        logger.debug(f"[feature_switch] 查询失败（回退 default）：{e}")
        return None


def is_enabled(switch_key: str, default: bool = False) -> bool:
    """开关是否启用。异常时回退 default。"""

    def _load():
        row = _query_one(
            "SELECT enabled FROM cfg_feature_switch WHERE switch_key = :k",
            {"k": switch_key})
        return bool(row[0]) if row is not None else default

    return bool(_cached(f"enabled:{switch_key}", _load))


def get_value(switch_key: str, default: Optional[str] = None) -> Optional[str]:
    """读取开关的 value（非布尔参数，如 lookback 天数）。异常/缺失回退 default。"""

    def _load():
        row = _query_one(
            "SELECT value FROM cfg_feature_switch WHERE switch_key = :k",
            {"k": switch_key})
        return row[0] if row is not None and row[0] is not None else default

    return _load() if default is not None else _cached(f"value:{switch_key}", _load)


def list_switches(switch_group: Optional[str] = None) -> list[dict]:
    """列出开关（可选按组过滤），用于看板/运维。异常返回空列表。"""
    try:
        with get_engine().connect() as conn:
            if switch_group:
                rows = conn.execute(
                    text("SELECT switch_key, switch_name, switch_group, enabled, value "
                         "FROM cfg_feature_switch WHERE switch_group = :g ORDER BY switch_key"),
                    {"g": switch_group}).fetchall()
            else:
                rows = conn.execute(
                    text("SELECT switch_key, switch_name, switch_group, enabled, value "
                         "FROM cfg_feature_switch ORDER BY switch_group, switch_key")).fetchall()
        return [dict(zip(("key", "name", "group", "enabled", "value"), r)) for r in rows]
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[feature_switch] list 失败：{e}")
        return []


def clear_cache() -> None:
    """测试 / 运维手动刷新缓存。"""
    _cache.clear()
