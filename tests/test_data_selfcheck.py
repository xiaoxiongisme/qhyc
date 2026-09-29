# -*- coding: utf-8 -*-
"""PRD V4 验收：数据质量自检（缺失 / 零成交 → 报警工单）。

验收点：
  * 注入「单日 bar 数缺失 > 5%」样本 → check_missing 写工单
  * 注入「成交量连续 0」样本     → check_zero_volume 写工单
  * enabled=False                → run() 直接跳过（return {"skipped": True}）

为不依赖真实库，使用一个内存级 FakeSession 承接 SQL 并返回注入样本；
断言落在"是否产生工单（计数 > 0）"上。切源逻辑本版未实现（仅留痕报警，
详见模块文档与 §9 遗留），此处只验报警链路。
"""
from __future__ import annotations

from datetime import date, timedelta

from app.ingest.data_selfcheck import (
    SelfCheckConfig,
    check_missing,
    check_zero_volume,
    run,
)


class _Result:
    def __init__(self, rows):
        self._rows = rows
    def fetchall(self):
        return self._rows
    def scalar(self):
        # 模拟 SQLAlchemy Result.scalar()：返回首行首列
        if not self._rows:
            return None
        first = self._rows[0]
        if isinstance(first, (tuple, list)):
            return first[0] if first else None
        return first


class FakeSession:
    """按 SQL 关键字返回注入样本的假 session；记录已写工单数量。"""

    def __init__(self, missing_rows=None, zero_rows=None, tail_max=None):
        self.missing_rows = missing_rows or []
        self.zero_rows = zero_rows or []
        self.tail_max = tail_max
        self.tickets = 0
        self.committed = 0

    def execute(self, sql, params=None):
        s = str(sql).lower()
        if "bucket::date" in s:          # check_missing 的缺失查询
            return _Result(self.missing_rows)
        if "volume = 0" in s:           # check_zero_volume 的零成交查询
            return _Result(self.zero_rows)
        if "max(bucket)" in s or "max(ts)" in s:   # 尾部回退查询
            return _Result([(self.tail_max,)] if self.tail_max is not None else [(None,)])
        if "insert into anomaly_ticket" in s:      # 写工单
            self.tickets += 1
            return _Result([])
        return _Result([])

    def commit(self):
        self.committed += 1
    def rollback(self):
        pass


def test_missing_triggers_ticket():
    """bar_15m 当日仅 10 根（预期 32，缺失 69% > 5%）→ 写 1 工单。"""
    cfg = SelfCheckConfig(enabled=True)
    # 让 check_missing 只在 bar_15m 看到 10 根，其余表无行
    sess = FakeSession(missing_rows=[("A888", 10)], zero_rows=[])
    # check_missing 会遍历 expected_bars 4 张表，都查 bar_15m/30m/60m/daily_bar；
    # 这里简化为只验证 bar_15m 命中。用 monkeypatch 让其余表返回空。
    n = check_missing(sess, cfg, date(2026, 9, 28))
    assert n >= 1, "缺失 69% 的样本未报警"


def test_zero_volume_triggers_ticket():
    """某品种连续 3 日成交量全 0 → 写 1 工单。"""
    cfg = SelfCheckConfig(enabled=True, zero_volume_days=3)
    # daily_bar 连续 N 日全零：z == c 即全零
    sess = FakeSession(zero_rows=[("B888", 3, 3)])
    n = check_zero_volume(sess, cfg, date(2026, 9, 28))
    assert n >= 1, "连续零成交样本未报警"


def test_disabled_skips():
    """enabled=False → run() 直接跳过，不写任何工单。"""
    sess = FakeSession(missing_rows=[("A888", 1)], zero_rows=[("B888", 3, 3)])
    res = run(sess, SelfCheckConfig(enabled=False), date(2026, 9, 28))
    assert res == {"skipped": True}
    assert sess.tickets == 0


def test_run_alerts_when_enabled():
    """enabled=True 且注入缺失样本 → run() 产生工单。"""
    sess = FakeSession(missing_rows=[("A888", 5)], zero_rows=[])
    res = run(sess, SelfCheckConfig(enabled=True), date(2026, 9, 28))
    assert res.get("skipped") is not True
    assert res["total"] >= 1


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
