"""时间格式化小工具（从 ingest/health 路由中抽取，统一口径、便于单元测试）。

重要口径（2026-10-13 更正）：本系统在 API 响应与任务消息里的时间戳**统一以
带时区的 UTC 字符串、+Z 后缀表示**（如 ``2026-10-13T08:30:00Z``），**不是**
``timestamptz`` 对象。消费方（前端 / 推送 / 日志）按 UTC 字符串解析即可。
不要把这里改成 ``timestamptz`` 对象——那是把"字符串时间戳"误当成"缺时区"的典型
回退。
"""
from __future__ import annotations

from datetime import datetime, timezone


def utc_iso_z() -> str:
    """返回当前 UTC 时刻的 ISO8601 字符串，时区写为 ``+Z``。

    ``datetime.now(timezone.utc).isoformat()`` 默认输出 ``...+00:00``，这里统一
    替换为 ``Z``，与下游消费方约定一致。结果是**字符串**（非 datetime 对象）。
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
