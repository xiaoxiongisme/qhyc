"""推送价位格式化的小数位来源契约（深模块边界测试）。

``scheduler._px_of`` 用于推送文案的价位展示。历史上用本地 ``_TICKS``/``_PX_DEC``
硬编码副本（缺失即静默按 2 位），与 ``dim_variety.tick_size`` 分叉（如大商所 P/Y
2026-04-01 由 2 改 1，副本不跟着变）。现改为读 ``barstore.variety_spec`` 的
``tick_size``（唯一真源），取不到才 fail-soft 兜底。

用 monkeypatch ``barstore.variety_spec`` 作本地替身，无需 DB。
"""
from __future__ import annotations

import pytest

import app.scheduler as sched


@pytest.fixture(autouse=True)
def _clear_dec_cache():
    sched._DEC_CACHE.clear()
    sched._DEC_WARNED.clear()
    yield
    sched._DEC_CACHE.clear()
    sched._DEC_WARNED.clear()


def test_px_of_uses_tick_half_step(monkeypatch):
    import app.data.barstore as bs
    monkeypatch.setattr(bs, "variety_spec", lambda p, **k: {"tick_size": 0.5})
    assert sched._px_of("J2601", 2012.34) == "2,012.3"
    assert sched._px_of("j888", 2012.34) == "2,012.3"  # 大小写/主连形态归一


def test_px_of_uses_tick_two_decimals(monkeypatch):
    import app.data.barstore as bs
    monkeypatch.setattr(bs, "variety_spec", lambda p, **k: {"tick_size": 0.02})
    assert sched._px_of("AU2606", 780.5) == "780.50"


def test_px_of_tick_none_returns_adaptive(monkeypatch):
    import app.data.barstore as bs
    monkeypatch.setattr(bs, "variety_spec", lambda p, **k: {"tick_size": None})
    assert sched._px_of("XX2601", 123.456) == "123.46"       # <10000 → 2 位
    assert sched._px_of("XX2601", 12345.6) == "12,345.6"     # >=10000 → 1 位


def test_px_of_fails_soft_when_variety_missing(monkeypatch):
    import app.data.barstore as bs
    from app.data.barstore import VarietySpecNotFoundError

    def _raise(_p, **_k):
        raise VarietySpecNotFoundError("missing")

    monkeypatch.setattr(bs, "variety_spec", _raise)
    assert sched._px_of("ZZZ2601", 123.456) == "123.46"  # 不崩，兜底


def test_px_of_none_and_bad_value():
    assert sched._px_of("J2601", None) == "-"
    assert sched._px_of("J2601", "abc") == "-"
    assert sched._px_of(None, None) == "-"


def test_tick_decimals_caches_per_variety(monkeypatch):
    import app.data.barstore as bs
    calls = {"n": 0}

    def _spec(_p, **_k):
        calls["n"] += 1
        return {"tick_size": 1}

    monkeypatch.setattr(bs, "variety_spec", _spec)
    assert sched._tick_decimals("FG") == 1
    assert sched._tick_decimals("FG") == 1
    assert calls["n"] == 1  # 第二次命中进程内缓存，不再查库
    assert sched._tick_decimals("") is None
