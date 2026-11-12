# -*- coding: utf-8 -*-
"""CL-20261112-12：作业心跳巡检对「字典行形状」演进的回归锁。

背景：CL-20261110-20 把 ops.scheduled_task 查询从 2 列扩到 5 列后，
``_check_job_heartbeat`` 仍用固定元组解包；精简列行抛 ValueError 被外层
except 整块吞掉 → task_meta={} → schedule=None → cron 感知**静默退化**成固定
9 天阈值 → 11-10 改期造成的 13 天合法空档被误报成「链路停摆」。

本测试只钉「实现」，不改既有测试：现行 5 列与旧版 2 列字典行都必须让改期
空档零告警，同时连续漏跑仍要报、字典不可读仍要按固定阈值报（不许静默）。
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.ops import alerting
from app.ops.alerting import AlertConfig, _check_job_heartbeat
from app.scheduler_jobs import JobSpec

CST = timezone(timedelta(hours=8))


def _d(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=CST)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Session:
    """按 SQL 关键字分发：字典查询返回 schedule 行，其余返回 task_run 行。"""

    def __init__(self, dict_rows, last):
        self._dict_rows = dict_rows
        self._last = last

    def execute(self, query, params=None):
        if "ops.scheduled_task" in str(query):
            return _Result(self._dict_rows)
        return _Result([("lstm_train", "weekly", self._last)])


def _freeze(monkeypatch, now: datetime) -> None:
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)

    monkeypatch.setattr(alerting, "datetime", _Frozen)
    monkeypatch.setattr(
        "app.scheduler_jobs.monitored_jobs",
        lambda: [JobSpec("lstm_weekly_retrain", "lstm_train", "weekly", 9, True, True)],
    )


@pytest.mark.parametrize("shape,dict_rows", [
    ("5列现行", [("lstm_weekly_retrain", "0 6 * * 5", 9, "", "apscheduler")]),
    ("2列精简", [("lstm_weekly_retrain", "0 6 * * 5")]),
])
def test_reschedule_gap_quiet_for_both_dict_shapes(monkeypatch, shape, dict_rows):
    """11-01 成功 / 现行 cron 周六 / now=11-10：合法空档不得告警。"""
    _freeze(monkeypatch, _d("2026-11-10 18:00"))
    out = _check_job_heartbeat(
        _Session(dict_rows, _d("2026-11-01 06:06")), AlertConfig())
    assert out == [], f"{shape} 字典行仍误报：{out}"


def test_two_missed_fires_still_alarms(monkeypatch):
    """弹性解析不能把真停摆也吞掉：连续错过两次周六触发必须告警。"""
    _freeze(monkeypatch, _d("2026-11-15 12:00"))
    out = _check_job_heartbeat(
        _Session([("lstm_weekly_retrain", "0 6 * * 5")], _d("2026-11-01 06:06")),
        AlertConfig())
    assert len(out) == 1 and "lstm_train" in out[0]["msg"]


def test_no_dict_still_falls_back_loud(monkeypatch):
    """字典不可读时回退固定阈值，停摆仍要能报出来。"""
    _freeze(monkeypatch, _d("2026-11-11 12:00"))
    out = _check_job_heartbeat(
        _Session([], _d("2026-11-01 06:06")), AlertConfig())
    assert len(out) == 1 and "固定阈值" in out[0]["msg"]
