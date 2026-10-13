# -*- coding: utf-8 -*-
"""作业心跳巡检（清单驱动重构）回归测试。

覆盖 2026-10-13 整改：心跳巡检改为直接读调度器注册清单（`app.scheduler_jobs`），
不再从 task_run 反推 + 关键字猜频率。核心要证明：
  1) 清单里没有的作业（退役 adjust、一次性手动回填 label）即使数据陈旧也**不误报**；
  2) 频率用清单里的显式 expected_gap_days，不再靠 'month'/'week'/'weights' 文本猜；
  3) 真正停摆的注册作业能正确告警；
  4) 健康作业零误报。
"""
from datetime import datetime, timedelta, timezone

from app.ops.alerting import AlertConfig, _check_job_heartbeat
from app.scheduler_jobs import JobSpec

CST = timezone(timedelta(hours=8))


def _now():
    return datetime.now(CST)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, query, params=None):
        return _FakeResult(self._rows)


def _reg(tt, lb, gap):
    return JobSpec("j_" + tt + "_" + lb, tt, lb, gap, True, True)


def _rows(specs):
    """specs: list of (task_type, label, days_ago) -> (task_type, label, finished_at)."""
    now = _now()
    out = []
    for tt, lb, d in specs:
        fa = (now - timedelta(days=d)).replace(tzinfo=CST) if d is not None else None
        out.append((tt, lb, fa))
    return out


def _run(monkeypatch, rows, reg_jobs):
    monkeypatch.setattr("app.scheduler_jobs.monitored_jobs", lambda: reg_jobs)
    cfg = AlertConfig()
    return _check_job_heartbeat(_FakeSession(rows), cfg)


# --------------------------------------------------------------------------
def test_healthy_no_false_alarm(monkeypatch):
    reg = [
        _reg("weights_update", "monthly", 40),
        _reg("inventory", "weekly", 4),
        _reg("lstm_train", "weekly", 9),
        _reg("backtest", "weekly", 9),
        _reg("transmission_calc", "weekly", 9),
        _reg("ingest", "pre_open", 3),
        _reg("predict", "pre_open", 3),
        _reg("spot_basis", "daily", 3),
    ]
    rows = _rows([
        ("weights_update", "monthly", 5),
        ("inventory", "weekly", 1),
        ("lstm_train", "weekly", 2),
        ("backtest", "weekly", 2),
        ("transmission_calc", "weekly", 2),
        ("ingest", "pre_open", 1),
        ("predict", "pre_open", 1),
        ("spot_basis", "daily", 1),
    ])
    out = _run(monkeypatch, rows, reg)
    assert out == [], f"健康作业不应告警，实际: {out}"


def test_retired_and_oneoff_labels_not_monitored(monkeypatch):
    """本次整改前的误报来源：退役 adjust + 一次性手动回填 label 陈旧也不应告警。"""
    reg = [
        _reg("weights_update", "monthly", 40),
        _reg("inventory", "weekly", 4),
        _reg("ingest", "pre_open", 3),
    ]
    rows = _rows([
        ("weights_update", "monthly", 5),
        ("inventory", "weekly", 1),
        ("ingest", "pre_open", 1),
        # 以下都不在注册清单里，即便陈旧也应被忽略
        ("adjust", "", 60),                              # 退役作业
        ("ingest", "manual_backfill_20261009", 30),     # 一次性手动 label
        ("predict", "manual-FG888", 25),                # 一次性手动 label
        ("ingest", "close", 40),                        # 已退役子标签
    ])
    out = _run(monkeypatch, rows, reg)
    assert out == [], f"退役/一次性 label 不应触发告警，实际: {out}"


def test_stale_monthly_alarms(monkeypatch):
    """月频作业停摆（>40 天）应告警，且阈值取自清单而非关键字猜。"""
    reg = [_reg("weights_update", "monthly", 40)]
    rows = _rows([("weights_update", "monthly", 50)])
    out = _run(monkeypatch, rows, reg)
    assert len(out) == 1
    assert "weights_update" in out[0]["msg"]
    assert "50.0" in out[0]["msg"]


def test_stale_inventory_alarms(monkeypatch):
    """库存（工作日盘后）超过 4 天未成功应告警。"""
    reg = [_reg("inventory", "weekly", 4)]
    rows = _rows([("inventory", "weekly", 5)])
    out = _run(monkeypatch, rows, reg)
    assert len(out) == 1
    assert "inventory" in out[0]["msg"]
    assert "5.0" in out[0]["msg"]


def test_inventory_friday_to_monday_no_false_alarm(monkeypatch):
    """周五→下周一约 3 天，阈值=4，不应误报。"""
    reg = [_reg("inventory", "weekly", 4)]
    rows = _rows([("inventory", "weekly", 3)])
    out = _run(monkeypatch, rows, reg)
    assert out == [], f"周末空档不应误报，实际: {out}"


def test_no_history_skipped(monkeypatch):
    """注册在跑但 task_run 无成功记录：与旧行为一致，不告警（防部署即误报）。"""
    reg = [_reg("weights_update", "monthly", 40)]
    rows = []  # task_run 里完全没有该作业
    out = _run(monkeypatch, rows, reg)
    assert out == [], f"无历史不应告警，实际: {out}"
