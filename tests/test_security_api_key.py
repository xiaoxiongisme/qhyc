# -*- coding: utf-8 -*-
"""API Key 鉴权验收（守卫式）。

覆盖四个真实分支，补此前的零覆盖：
  * 占位符 key         → 放行（verify_api_key 返回 None），与未启用鉴权逐位等价
  * 真实 key + 缺失头  → 403
  * 真实 key + 错误值  → 403
  * 真实 key + 正确值  → 原样返回

另覆盖两个派生契约：
  * auth_enabled() 对占位符清单的判定
  * 鉴权启用后 /docs 与 /openapi.json 自动隐藏（防止"配了 key 仍公开接口清单"）
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.core import security
from app.core.security import auth_enabled, verify_api_key

REAL_KEY = "unit_test_real_key_32bytes_min_len"


class _FakeEnv:
    def __init__(self, key: str) -> None:
        self.INTEGRATION_API_KEY = key


class _FakeSettings:
    def __init__(self, key: str) -> None:
        self.env = _FakeEnv(key)


def _set_key(monkeypatch, value: str) -> None:
    """注入 key。

    不用 monkeypatch.setenv + cache_clear：``get_settings()`` 内部还套了
    ``get_env()`` 的独立 lru_cache，只清外层拿不到新值。这里直接替换
    ``security.get_settings``——被测单元是鉴权判据本身，与配置加载解耦更稳。
    """
    monkeypatch.setattr(security, "get_settings", lambda: _FakeSettings(value))


def _call(key):
    return asyncio.run(verify_api_key(key))


# --- 1) 占位符 ⇒ 放行（向后兼容，未启用鉴权不改行为） -------------------------

@pytest.mark.parametrize(
    "placeholder",
    ["", "changeme", "your-api-key-here", "FILL_ME_IN", "placeholder",
     "dev_placeholder", "dev_placeholder_change_before_m5"],
)
def test_placeholder_key_passthrough(monkeypatch, placeholder):
    _set_key(monkeypatch, placeholder)
    assert auth_enabled() is False
    assert _call(None) is None
    assert _call("whatever") is None  # 即便带了头也放行（未启用）


# --- 2) 真实 key 的三个分支 ---------------------------------------------------

def test_real_key_missing_header_raises_403(monkeypatch):
    _set_key(monkeypatch, REAL_KEY)
    assert auth_enabled() is True
    with pytest.raises(HTTPException) as ei:
        _call(None)
    assert ei.value.status_code == 403


def test_real_key_wrong_value_raises_403(monkeypatch):
    _set_key(monkeypatch, REAL_KEY)
    with pytest.raises(HTTPException) as ei:
        _call(REAL_KEY + "_wrong")
    assert ei.value.status_code == 403


def test_real_key_correct_passes(monkeypatch):
    _set_key(monkeypatch, REAL_KEY)
    assert _call(REAL_KEY) == REAL_KEY


def test_real_key_trailing_space_treated_as_placeholder_family(monkeypatch):
    """空白串等价于未配置（.env 里 `KEY=` 常见），不应拦截。"""
    _set_key(monkeypatch, "   ")
    assert auth_enabled() is False


# --- 3) 派生契约：鉴权启用后文档自动隐藏 -------------------------------------

def test_docs_hidden_when_auth_enabled(monkeypatch):
    _set_key(monkeypatch, REAL_KEY)
    from app.main import create_app

    app = create_app()
    assert app.docs_url is None
    assert app.openapi_url is None


def test_docs_visible_when_auth_disabled(monkeypatch):
    _set_key(monkeypatch, "dev_placeholder")
    from app.main import create_app

    app = create_app()
    assert app.docs_url == "/docs"
    assert app.openapi_url == "/openapi.json"
