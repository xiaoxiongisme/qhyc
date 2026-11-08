# -*- coding: utf-8 -*-
"""CL-20261109-04：休市期的合成 bar 必须在采集器入口被丢弃。

事故现场（2026-11-09 00:20）：``l0_raw.minute_bar`` 出现 11 根
``ts = 2026-11-09 00:00`` 的 continuous bar（AG/AL/AO/AU/CU/NI/PB/SC/SN/SS/ZN），
全部 ``src=akshare_1min``。直接调 akshare 复现：``ag0`` 收盘于 11-07 02:30 后
紧跟一根 2026-11-09 00:00（volume=121、OHLC 齐全）—— 而 11-09 是周一，
**周一凌晨无夜盘**。这根 bar 会一路进 bar_60m → bar_60m_ba（V5X 唯一信号源），
占住状态机「上一根」的位置参与 EMA/ATR/开平判定，且全程不报错。

本组测试钉两件事：
  1. 时间戳落在非交易时段的 bar 被丢弃；
  2. **合法 bar 一根都不能少**（这条更重要：判据写严了会把历史窗口整体误杀，
     而 akshare 每次返回 1024 根历史，误杀等于当天全品种无数据）。
"""
from __future__ import annotations

import sys
import types
from zoneinfo import ZoneInfo

import pandas as pd

from app.core.config import MainContractSpec
from app.ingest.minute_collector import MinuteCollector

SH = ZoneInfo("Asia/Shanghai")

SPEC = MainContractSpec(
    symbol="AG888", exchange="SHFE", product="AG", name="白银",
    unit="元/千克", multiplier=15,
)


def _row(dt: str, close: float = 12_500.0) -> dict:
    return {
        "datetime": dt,
        "open": close, "high": close + 2, "low": close - 2, "close": close,
        "volume": 10, "hold": 1000,
    }


def _patch_ak(monkeypatch, rows: list[dict]) -> None:
    mod = types.ModuleType("akshare")
    mod.futures_zh_minute_sina = lambda symbol, period="1": pd.DataFrame(rows)
    monkeypatch.setitem(sys.modules, "akshare", mod)


def _fetch(monkeypatch, rows: list[dict]) -> list[dict]:
    _patch_ak(monkeypatch, rows)
    mc = MinuteCollector.__new__(MinuteCollector)     # 绕过 __init__（不碰 DB）
    return mc._fetch_akshare(SPEC)


def _sh(ts) -> str:
    return ts.astimezone(SH).strftime("%Y-%m-%d %H:%M")


def test_drops_monday_before_open_bar(monkeypatch):
    """周一凌晨（无夜盘）的 bar 必须丢弃，同批合法 bar 必须保留。"""
    rows = [
        _row("2026-11-07 02:29:00"),
        _row("2026-11-07 02:30:00"),   # 周五夜盘最后一根 —— 合法
        _row("2026-11-09 00:00:00"),   # 幽灵：周一凌晨
    ]
    got = [_sh(r["ts"]) for r in _fetch(monkeypatch, rows)]
    assert got == ["2026-11-07 02:29", "2026-11-07 02:30"], got


def test_keeps_tuesday_early_morning_bar(monkeypatch):
    """周二的 01:00 属于周一 21:00 起的夜盘 → 必须保留。

    这是最容易写反的一处：若判据「凌晨一律丢弃」，每个交易日的夜盘下半段全没了。
    日期取 2026-11-03（周二，历史日）——不能取未来日期，否则会先被
    ``_valid_minute_row`` 的「未来戳」规则剔除，测的就变成另一条逻辑了。
    """
    rows = [
        _row("2026-11-03 00:59:00"),
        _row("2026-11-03 01:00:00"),
        _row("2026-11-03 02:30:00"),   # 夜盘收盘那根
    ]
    got = [_sh(r["ts"]) for r in _fetch(monkeypatch, rows)]
    assert got == ["2026-11-03 00:59", "2026-11-03 01:00", "2026-11-03 02:30"], got


def test_keeps_normal_day_session(monkeypatch):
    """日盘常规 bar 不受影响。"""
    rows = [_row("2026-11-06 09:01:00"), _row("2026-11-06 10:00:00"),
            _row("2026-11-06 14:59:00"), _row("2026-11-06 15:00:00")]
    got = [_sh(r["ts"]) for r in _fetch(monkeypatch, rows)]
    assert len(got) == 4, got


def test_drops_sunday_bar(monkeypatch):
    """周日全天休市。"""
    rows = [_row("2026-11-08 10:00:00"), _row("2026-11-07 02:30:00")]
    got = [_sh(r["ts"]) for r in _fetch(monkeypatch, rows)]
    assert got == ["2026-11-07 02:30"], got


def test_drops_after_hours_gap(monkeypatch):
    """15:20–20:50 的盘后空档期同样不该有 bar。"""
    rows = [_row("2026-11-06 15:00:00"), _row("2026-11-06 16:30:00"),
            _row("2026-11-06 20:50:00")]
    got = [_sh(r["ts"]) for r in _fetch(monkeypatch, rows)]
    assert got == ["2026-11-06 15:00", "2026-11-06 20:50"], got
