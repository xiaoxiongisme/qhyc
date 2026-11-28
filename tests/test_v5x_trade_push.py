# -*- coding: utf-8 -*-
"""CL-20261128-03：交易推送只含交易信息，系统状态另发一条。"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

from app.strategies.v5x import brief, snapshot
from app.strategies.v5x.live import V5XSignal, format_message

LABEL = datetime(2026, 11, 28, 22, 0)
POS = [{"symbol": "MA2701", "dir": "LONG", "lots": 1,
        "stop_real": 2500.0, "unrealized_yuan": 110.0, "be_done": False}]


def _signal() -> V5XSignal:
    return V5XSignal(symbol="SC2701", action="OPEN", side="LONG",
                     bar_ts=LABEL, adj_offset=0.0, price_ba=520.0,
                     price_real=520.0, stop_real=500.0, lots=1.0, etype="bk")


def test_cumulative_anchor_and_sql():
    assert snapshot.PNL_SINCE == date(2026, 11, 2)
    sql = snapshot.pnl_since_sql()
    assert "sum(net_yuan)" in sql and "bar_ts >= :since" in sql

    class _R:
        def scalar(self):
            return 123.45

    class _S:
        def execute(self, _q, p):
            assert p["since"] == date(2026, 11, 2)
            return _R()

    assert snapshot.cumulative_signal_pnl(_S()) == 123.45


def test_format_message_trade_only():
    _, body = format_message([_signal()], positions=POS, cumulative=-777.21)
    for needle in ("当前持仓：", "MA2701", "盈亏：+110元", "止损：2500.0",
                   "持仓手数：1手", "交易计划：", "SC2701", "方向：多",
                   "交易点位：520.00", "止损点位：500.00", "买入数量：1手",
                   "自2026年11月2日以来，累计交易信号盈亏：-777元"):
        assert needle in body, needle
    for word in ("分钟覆盖", "采集 ", "分批：", "待补发", "下一根收盘", "口径："):
        assert word not in body, word


def test_format_message_without_positions_stays_legacy():
    """纯函数历史调用（守卫/旧单测）不传持仓时不渲染该段。"""
    _, body = format_message([_signal()])
    assert "当前持仓：" not in body
    assert "累计交易信号盈亏" not in body
    assert "交易计划：" in body


def test_build_brief_has_no_trade_fields():
    _, body = brief.build_brief(None, LABEL, {
        "scan": {}, "coverage": (0, 0), "symbols": 38})
    assert "当前持仓" not in body
    assert "累计交易信号盈亏" not in body
    assert "扫描" in body


def test_build_trade_brief_sections(monkeypatch):
    monkeypatch.setattr(snapshot, "theoretical_positions",
                        lambda _s: {"rows": POS})
    monkeypatch.setattr(snapshot, "cumulative_signal_pnl", lambda _s: -777.21)
    title, body = brief.build_trade_brief(None, LABEL)
    assert title.startswith("[交易信息]")
    assert "MA2701" in body and "累计交易信号盈亏：-777元" in body
    for word in ("分钟覆盖", "采集 ", "下一根收盘"):
        assert word not in body


def test_push_pair_sends_two_messages(monkeypatch):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(brief, "_signal_pushed_for_hour", lambda _s, _l: False)
    monkeypatch.setattr(brief, "_trade_seen", lambda _k: False)
    monkeypatch.setattr(brief, "_brief_seen_kind", lambda _k, _k2: False)
    monkeypatch.setattr(brief, "_close_brief_present_for_hour", lambda _s, _l: False)
    monkeypatch.setattr(brief, "_claim_trade", lambda _k: True)
    monkeypatch.setattr(brief, "_claim_brief_kind", lambda _k, _k2: True)
    monkeypatch.setattr(brief, "build_trade_brief",
                        lambda _s, _l: ("[交易信息]", "trade body"))
    monkeypatch.setattr(brief, "build_brief",
                        lambda _s, _l, _sum: ("[系统状态]", "system body"))
    monkeypatch.setattr("app.notify.send_notify",
                        lambda t, c, f: (sent.append((t, c)) or (True, "pushplus")))
    monkeypatch.setattr("app.core.config.get_settings",
                        lambda: SimpleNamespace(fusion=SimpleNamespace(fallback_webhook="")))
    monkeypatch.setattr("app.ingest.trading_window.is_hourly_collection_allowed",
                        lambda _t: True)

    res = brief.push_periodic_brief(object(), LABEL, {}, do_push=True)
    assert res["sent"] is True and res["trade_sent"] and res["system_sent"]
    assert [t for t, _ in sent] == ["[交易信息]", "[系统状态]"]
