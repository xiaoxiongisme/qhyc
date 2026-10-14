"""scheduler._switch_on 功能开关读取契约（job 注册门 + 防静默失效）。

``_switch_on`` 决定哪些定时 job 是否注册（factor_v1v6 / data_selfcheck /
portfolio_brake …），规则：**优先 cfg_feature_switch（DB）**；表缺失 / 连接失败 /
键不存在时回退 env；再回退 default；**回退路径绝不抛错**（熔断失效可以，误杀不行）。

这正是本项目最重的缺陷类型「开关看似生效、实则取不到」的风险面，历史零覆盖。
用 fake engine 注入 DB 读取 + monkeypatch os.environ，无需真实 DB。
"""
from __future__ import annotations

import pytest

import app.scheduler as sched


class _Result:
    def __init__(self, val):
        self._val = val

    def scalar(self):
        return self._val


class _Conn:
    def __init__(self, val=None, raise_exc=False):
        self._val = val
        self._raise = raise_exc

    def execute(self, *a, **k):
        if self._raise:
            raise RuntimeError("relation cfg_feature_switch does not exist")
        return _Result(self._val)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Engine:
    def __init__(self, conn):
        self._conn = conn

    def connect(self):
        return self._conn


def _patch_engine(monkeypatch, val=None, raise_exc=False):
    monkeypatch.setattr(sched, "get_engine", lambda: _Engine(_Conn(val, raise_exc)))


# ---------- DB 优先 ----------
def test_db_true_wins(monkeypatch):
    _patch_engine(monkeypatch, val=True)
    monkeypatch.delenv("MY_SW", raising=False)
    assert sched._switch_on("my_switch", "MY_SW", default=False) is True


def test_db_false_wins_over_env(monkeypatch):
    # DB 显式 false 必须压过 env=1（DB 优先，避免 env 覆盖库内关闭）
    _patch_engine(monkeypatch, val=False)
    monkeypatch.setenv("MY_SW", "1")
    assert sched._switch_on("my_switch", "MY_SW", default=True) is False


# ---------- 回退 env ----------
def test_env_fallback_when_key_absent(monkeypatch):
    # 表在但无此键（row=None）→ 回退 env
    _patch_engine(monkeypatch, val=None)
    monkeypatch.setenv("MY_SW", "1")
    assert sched._switch_on("my_switch", "MY_SW", default=False) is True
    monkeypatch.setenv("MY_SW", "0")
    assert sched._switch_on("my_switch", "MY_SW", default=True) is False


def test_env_fallback_when_table_missing_never_raises(monkeypatch):
    # 表不存在（异常）→ 回退 env，且绝不抛错
    _patch_engine(monkeypatch, raise_exc=True)
    monkeypatch.setenv("MY_SW", "1")
    assert sched._switch_on("my_switch", "MY_SW", default=False) is True


def test_env_unset_uses_default(monkeypatch):
    _patch_engine(monkeypatch, val=None)
    monkeypatch.delenv("MY_SW", raising=False)
    assert sched._switch_on("my_switch", "MY_SW", default=True) is True
    assert sched._switch_on("my_switch", "MY_SW", default=False) is False


# ---------- 无 env_name → default ----------
def test_no_env_name_returns_default(monkeypatch):
    _patch_engine(monkeypatch, val=None)
    assert sched._switch_on("my_switch", None, default=True) is True
    assert sched._switch_on("my_switch", None, default=False) is False
