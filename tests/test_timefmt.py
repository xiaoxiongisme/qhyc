"""timefmt 遗留代码 characterization 测试。

锁定 ingest/health 的时间戳口径（2026-10-13 更正）：写入的是**字符串时间戳**
（带时区 UTC、+Z 后缀），**不是** timestamptz 对象。这条测试防止有人把
utc_iso_z() 误改成返回 datetime / timestamptz——那样会破坏 API 消费方（按 UTC
字符串解析）与任务消息（文本字段）的约定。
"""
from __future__ import annotations

from app.core.timefmt import utc_iso_z


def test_utc_iso_z_returns_string_not_datetime():
    v = utc_iso_z()
    # 关键：是字符串，不是 datetime 对象 / timestamptz
    assert isinstance(v, str)
    assert not isinstance(v, __import__("datetime").datetime)


def test_utc_iso_z_has_z_suffix_and_no_offset():
    v = utc_iso_z()
    assert v.endswith("Z"), f"应带 +Z 后缀表示 UTC，实际: {v}"
    assert "+00:00" not in v, f"不应残留 +00:00 偏移写法，实际: {v}"


def test_utc_iso_z_is_utc_nowish():
    from datetime import datetime, timezone
    before = datetime.now(timezone.utc)
    v = utc_iso_z()
    after = datetime.now(timezone.utc)
    # 解析回 UTC 后应在 [before, after] 区间内（容差几秒）
    parsed = datetime.fromisoformat(v.replace("Z", "+00:00"))
    assert before <= parsed <= after
    assert parsed.utcoffset().total_seconds() == 0
