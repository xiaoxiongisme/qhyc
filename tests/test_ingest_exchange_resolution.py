"""品种→交易所解析的单一真源契约（深模块边界测试）。

``AkShareSource.detect_exchange`` 是全平台「品种→交易所」的唯一解析入口
（底层读 ``dim_variety.exchange``）。历史上 ``local_importer._PRODUCT_EXCHANGE``
等 4 份硬编码副本在缺失时**静默返回 UNKNOWN**，导致采集走错分支。

本测试锁定其 fail-loud 契约：库内取到即返回；库内缺失/异常**必须抛**
（绝不静默 UNKNOWN）。用 monkeypatch ``barstore.variety_spec`` 作本地替身，
无需真实 DB。
"""
from __future__ import annotations

import pytest

from app.core.exceptions import IngestError
from app.ingest.akshare_source import AkShareSource


def test_detect_exchange_returns_dim_variety_value(monkeypatch):
    import app.data.barstore as bs
    monkeypatch.setattr(bs, "variety_spec", lambda p, **k: {"exchange": "DCE"})
    # 大小写/形态归一由 variety_of 负责，这里只验证透传
    assert AkShareSource.detect_exchange("M") == "DCE"
    assert AkShareSource.detect_exchange("m") == "DCE"


def test_detect_exchange_fails_loud_when_variety_missing(monkeypatch):
    """库内查不到品种 → 必须抛 IngestError，绝不返回 'UNKNOWN'。"""
    import app.data.barstore as bs
    from app.data.barstore import VarietySpecNotFoundError

    def _raise(_p, **_k):
        raise VarietySpecNotFoundError("no such variety")

    monkeypatch.setattr(bs, "variety_spec", _raise)
    with pytest.raises(IngestError):
        AkShareSource.detect_exchange("ZZZ")


def test_detect_exchange_fails_loud_on_empty_product():
    with pytest.raises(IngestError):
        AkShareSource.detect_exchange("")
    with pytest.raises(IngestError):
        AkShareSource.detect_exchange(None)


def test_local_importer_no_longer_carries_exchange_dict():
    """回归护栏：本地交易所副本已删除，import_product 改走唯一真源。"""
    import app.ingest.local_importer as li
    assert not hasattr(li, "_PRODUCT_EXCHANGE"), (
        "本地 _PRODUCT_EXCHANGE 副本应已删除；交易所解析统一走 "
        "AkShareSource.detect_exchange → dim_variety.exchange")
