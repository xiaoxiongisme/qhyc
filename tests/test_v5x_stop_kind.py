# -*- coding: utf-8 -*-
"""CL-20261124-05：V5X 离场原因细分（吊灯/初始/保本止损）。

钉死三件事：
  1. ``classify_stop_kind`` 与 on_bar 三条候选线逐位同式（六例对拍）；
  2. 推送文案按 stop_kind 精确渲染，历史行（stop_kind=None）回退旧文案；
  3. stop_kind 经落库 SQL / 读回映射真实透传（不是只写在内存里）。

关键边界：绝不能用 net_yuan>0 反推吊灯——吊灯触发但扣费后小亏、保本锁盈
都会误标。本文件用「机制与盈亏正交」的样例钉死这一点。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.strategies.v5x.engine import V5XConfig, classify_stop_kind
from app.strategies.v5x.live import V5XSignal, format_message

SH = timezone(__import__("datetime").timedelta(hours=8))


def _cfg():
    return V5XConfig()


# ---------------------------------------------------------------- 纯判据对拍
def test_classify_long_initial_stop():
    """多头：峰值不够高，初始止损最紧 → initial。"""
    cfg = _cfg()
    avg, atr, entry = 1000.0, 10.0, 1000.0
    extreme = 1000.0                       # t = 1000-20 = 980 < st_init=982.5 → initial
    assert classify_stop_kind(1, avg, extreme, atr, entry, False, cfg) == "initial"


def test_classify_long_trail_stop():
    """多头：峰值足够高，吊灯线反超初始 → trail（Y888 同类）。"""
    cfg = _cfg()
    avg, atr, entry = 1000.0, 10.0, 1000.0
    extreme = 1060.0                       # t = 1060-20 = 1040 > st_init=982.5
    assert classify_stop_kind(1, avg, extreme, atr, entry, False, cfg) == "trail"


def test_classify_long_be_stop():
    """多头：保本激活且 entry 更紧 → be。"""
    cfg = _cfg()
    avg, atr, entry = 1000.0, 10.0, 1050.0
    extreme = 1060.0                       # t=1040 > st_init=982.5，但 entry=1050 更紧 → be
    assert classify_stop_kind(1, avg, extreme, atr, entry, True, cfg) == "be"


def test_classify_short_initial_stop():
    """空头：谷值不够低，初始止损最紧 → initial。"""
    cfg = _cfg()
    avg, atr, entry = 1000.0, 10.0, 1000.0
    extreme = 1000.0                       # t = 1000+20 = 1020 > st_init=1017.5 → initial
    assert classify_stop_kind(-1, avg, extreme, atr, entry, False, cfg) == "initial"


def test_classify_short_trail_stop():
    """空头：谷值足够低，吊灯线反超 → trail（Y888 实际情形）。"""
    cfg = _cfg()
    avg, atr, entry = 8735.0, 37.57, 8764.0
    extreme = 8441.0                       # t = 8441+2*37.57 = 8516.14 < st_init=8800.75
    assert classify_stop_kind(-1, avg, extreme, atr, entry, False, cfg) == "trail"


def test_classify_short_be_stop():
    """空头：保本激活且 entry 更紧 → be。"""
    cfg = _cfg()
    avg, atr, entry = 1000.0, 10.0, 950.0
    extreme = 940.0                        # t = 960 < st_init=1017.5，但 entry=950 更紧 → be
    assert classify_stop_kind(-1, avg, extreme, atr, entry, True, cfg) == "be"


def test_classify_matches_on_bar_arithmetic():
    """对拍：与 on_bar 第 345-350 行的取值链逐位相同（多头）。"""
    cfg = _cfg()
    avg, atr, entry, peak, be_done = 1000.0, 10.0, 1000.0, 1060.0, False
    st = avg - cfg.SL_ATR * atr            # 982.5
    t = peak - cfg.TRAIL_ATR * atr         # 1040
    if t > st:
        st = t
    if be_done and entry > st:
        st = entry
    # 生效的是 t（吊灯）→ st=1040
    assert st == 1040.0
    assert classify_stop_kind(1, avg, peak, atr, entry, be_done, cfg) == "trail"


# ---------------------------------------------------------------- 文案渲染
def _close_sig(**kw):
    base = dict(
        symbol="Y888", action="CLOSE", side="SHORT",
        bar_ts=datetime(2026, 11, 24, 15, 0, tzinfo=SH),
        adj_offset=32.0, price_ba=8521.0, price_real=8489.0,
        lots=2.0, stop_real=None, reason="stop", stop_kind=None,
        net_yuan=4229.96,
    )
    base.update(kw)
    return V5XSignal(**base)


def test_message_trail_stop_label():
    title, body = format_message([_close_sig(stop_kind="trail")])
    assert "离场原因：吊灯移动止损（锁盈）" in body
    assert "离场原因：止损\n" not in body


def test_message_be_stop_label():
    _, body = format_message([_close_sig(stop_kind="be")])
    assert "离场原因：保本止损（锁盈）" in body


def test_message_initial_stop_label():
    _, body = format_message([_close_sig(stop_kind="initial", net_yuan=-800.0)])
    assert "离场原因：初始止损" in body


def test_message_history_row_falls_back():
    """历史行 stop_kind=None → 回退旧「止损」文案，不改历史留痕。"""
    _, body = format_message([_close_sig(stop_kind=None)])
    assert "离场原因：止损" in body
    assert "吊灯" not in body


def test_message_tp_not_relabelled():
    """止盈单不受影响：reason=tp 即便误带 stop_kind 也不改标签。"""
    _, body = format_message([_close_sig(reason="tp", stop_kind="trail", net_yuan=100.0)])
    assert "离场原因：止盈" in body
    assert "吊灯" not in body


def test_mechanism_independent_of_pnl():
    """机制与盈亏正交：吊灯触发但净亏仍标吊灯，不靠 net 反推。"""
    _, body = format_message([_close_sig(stop_kind="trail", net_yuan=-50.0)])
    assert "吊灯移动止损" in body        # 净亏也不改机制标签
    _, body2 = format_message([_close_sig(stop_kind="initial", net_yuan=5000.0)])
    assert "初始止损" in body2            # 净盈也不冒充吊灯


# ---------------------------------------------------------------- 字段透传
def test_orders_to_signals_carries_stop_kind():
    """引擎 order dict 的 stop_kind 必须进入 V5XSignal，不能只在内存里丢失。"""
    from app.strategies.v5x.live import _to_signals

    orders = [dict(type="CLOSE", dir=-1, price=8521.0, lots=2.0,
                   ts=datetime(2026, 11, 24, 15, 0, tzinfo=SH),
                   net=4229.96, reason="stop", stop_kind="trail")]
    sigs = _to_signals("Y888", datetime(2026, 11, 24, 15, 0, tzinfo=SH),
                       32.0, orders, source="live")
    assert len(sigs) == 1
    assert sigs[0].stop_kind == "trail"
    assert sigs[0].reason == "stop"


def test_persist_sql_writes_stop_kind_and_readback_maps_it():
    """落库 INSERT 与读回 SELECT 都必须带 stop_kind——守卫之外的最小真实引用。"""
    import inspect
    from app.strategies.v5x import live as L

    persist_src = inspect.getsource(L._persist)
    assert "stop_kind" in persist_src
    assert ":stop_kind" in persist_src
    assert '"stop_kind": s.stop_kind' in persist_src

    unsent_src = inspect.getsource(L._load_unsent)
    assert "stop_kind" in unsent_src
    # 兼容旧行/缺列：用 keys() 检查而非直接索引
    assert '"stop_kind" in r.keys()' in unsent_src
