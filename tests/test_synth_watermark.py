# -*- coding: utf-8 -*-
"""CL-20261125-04：L1 合成增量水位单测（纯离线，不依赖 DB）。

锁三件事：
  1. 冷启动窗口逐位等于改造前的 floor(now - days_back - 1天)；
  2. 有水位时窗口 = floor(covered - overlap)，且比冷启动更短（减负实证）；
  3. 水位前移到未来时防御夹回，绝不产生 since > now（前移漏桶的第一道闸）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.ingest.synthesizer import wm_since

TZ = timezone(timedelta(hours=8))


def test_cold_start_matches_legacy_window():
    """无水位 = 改造前口径，逐位一致（零风险灰度）。"""
    now = datetime(2026, 11, 25, 14, 47, tzinfo=TZ)
    since, src = wm_since(None, 1.0, now, days_back=3)
    # 旧口径：floor((now-3天)-1天) = floor(11-21 14:47) = 11-21 09:00
    assert src == "cold_start"
    assert since == datetime(2026, 11, 21, 9, 0, tzinfo=TZ)


def test_watermark_window_is_shorter_than_legacy():
    """有水位时窗口收缩——这是减负的核心，且会话对齐到 09:00。"""
    now = datetime(2026, 11, 25, 14, 47, tzinfo=TZ)
    covered = datetime(2026, 11, 25, 14, 45, tzinfo=TZ)  # 刚追平
    since, src = wm_since(covered, 1.0, now, days_back=3)
    assert src == "watermark"
    # floor(11-25 14:45 - 1天) = floor(11-24 14:45) = 11-24 09:00
    assert since == datetime(2026, 11, 24, 9, 0, tzinfo=TZ)
    cold, _ = wm_since(None, 1.0, now, 3)
    assert since > cold  # 水位窗口起点更晚 = 窗口更短


def test_watermark_aligned_across_day_boundary():
    """水位落在夜盘末(02:30)时，floor 必须对齐到会话起点而非停在凌晨。"""
    now = datetime(2026, 11, 25, 10, 30, tzinfo=TZ)
    covered = datetime(2026, 11, 25, 2, 30, tzinfo=TZ)  # 夜盘收
    since, src = wm_since(covered, 1.0, now)
    assert src == "watermark"
    # floor(11-24 02:30) = 11-23 09:00（02:30 < 09:00，回退到前一日日盘起点）
    assert since == datetime(2026, 11, 23, 9, 0, tzinfo=TZ)


def test_future_watermark_is_clamped():
    """水位跑到未来（时钟异常/误写）时夹回 now-1天，绝不 since>now。"""
    now = datetime(2026, 11, 25, 14, 47, tzinfo=TZ)
    covered = datetime(2026, 12, 1, 10, 0, tzinfo=TZ)  # 未来 6 天
    since, src = wm_since(covered, 1.0, now)
    assert src == "watermark"
    assert since <= now
    assert since == datetime(2026, 11, 24, 9, 0, tzinfo=TZ)


def test_lookback_grows_on_stalled_watermark():
    """水位停滞多天（挂机/漏跑）时，窗口自动扩大覆盖缺口——不漏桶。"""
    now = datetime(2026, 11, 25, 14, 47, tzinfo=TZ)
    stalled = datetime(2026, 11, 20, 15, 0, tzinfo=TZ)  # 停了 5 天
    since, src = wm_since(stalled, 1.0, now)
    # floor(11-19 15:00) = 11-19 09:00，把 20~25 的缺口全纳入
    assert since == datetime(2026, 11, 19, 9, 0, tzinfo=TZ)
    assert since < datetime(2026, 11, 20, 9, 0, tzinfo=TZ)
