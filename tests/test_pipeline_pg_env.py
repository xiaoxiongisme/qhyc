"""M8 管线子进程 QH_PG_* 兜底的单一真源契约。

``worker._pg_env_fallback`` 从应用同一配置源 ``settings.database_url()`` 派生
子进程连接参数。回归护栏：**绝不再自带 ``127.0.0.1`` host 默认**（旧实现与
``app.core.config`` 的 ``POSTGRES_HOST=timescaledb`` 分叉 → 子进程静默连错库）。
"""
from __future__ import annotations

from types import SimpleNamespace

import app.pipeline.worker as w


def _patch_settings(monkeypatch, url: str):
    # ⚠ 2026-10-28 修正：mock 曾停在 `SimpleNamespace(database_url=...)`，
    # 而真实配置结构是 `Settings.env: EnvSettings`、``database_url()`` 在 EnvSettings 上
    # （见 app/core/config.py: EnvSettings.database_url / Settings.env）。
    # 即 **代码是对的、mock 过期了**，3 条用例因此长期红灯、并给出"worker 有 bug"的假信号。
    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: SimpleNamespace(env=SimpleNamespace(database_url=lambda: url)))


def test_derives_from_discrete_config(monkeypatch):
    _patch_settings(monkeypatch, "postgresql+psycopg://futures:pw@timescaledb:5432/futures")
    assert w._pg_env_fallback() == {
        "QH_PG_HOST": "timescaledb", "QH_PG_PORT": "5432", "QH_PG_DB": "futures",
        "QH_PG_USER": "futures", "QH_PG_PWD": "pw",
    }


def test_derives_from_database_url_override(monkeypatch):
    # 配置来自整体 DATABASE_URL 时，也以应用实际连接的库为准
    _patch_settings(monkeypatch, "postgresql+psycopg://u:p@10.0.0.5:6543/mydb")
    fb = w._pg_env_fallback()
    assert fb["QH_PG_HOST"] == "10.0.0.5"
    assert fb["QH_PG_PORT"] == "6543"
    assert fb["QH_PG_DB"] == "mydb"
    assert fb["QH_PG_USER"] == "u"


def test_never_defaults_host_to_localhost(monkeypatch):
    # 关键回归护栏：host 来自配置源，绝不擅自回落 127.0.0.1；空密码保留为空
    _patch_settings(monkeypatch, "postgresql+psycopg://futures:@timescaledb:5432/futures")
    fb = w._pg_env_fallback()
    assert fb["QH_PG_HOST"] == "timescaledb"
    assert fb["QH_PG_HOST"] != "127.0.0.1"
    assert fb["QH_PG_PWD"] == ""
