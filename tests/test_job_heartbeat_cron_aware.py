# -*- coding: utf-8 -*-
"""CL-20261110-10 A：作业心跳 cron 感知判据回归测试。

背景：2026-11-10 用户收到两条「已 9.4 天无成功运行（阈值 9 天）—— 采集/调度
链路可能停摆」。核因是 CL-20261107-03 把周频 cron 由错位的周日改回周六，
11-01（周日）最后一次成功后，下一次正常触发变成 11-14，形成 13 天**合法**
过渡空档；日频链路当时全部 fresh。固定天数阈值把「合法空档」误判成「链路停摆」。

本测试锁住三件事：
  1) 改期过渡空档不告警，但连续错过两次应触发必须告警；
  2) 字典无 cron / cron 解析失败 / 超过硬上限时仍能告警（不许静默）；
  3) 判据被 `_check_job_heartbeat` 真实接线，且日频仍沿用固定阈值。
"""
from datetime import datetime, timedelta, timezone

from app.ops import alerting
from app.ops.alerting import AlertConfig, _check_job_heartbeat, job_heartbeat_verdict
from app.scheduler_jobs import JobSpec

CST = timezone(timedelta(hours=8))


def _d(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=CST)


CASES = [
    # name, last, now, schedule, gap, want
    ("改期过渡空档不误报", "2026-11-01 06:06", "2026-11-10 18:00", "0 6 * * 5", 9, False),
    ("连续错过两次应触发告警", "2026-11-01 06:06", "2026-11-15 12:00", "0 6 * * 5", 9, True),
    ("补跑后正常周期内不误报", "2026-11-10 17:16", "2026-11-12 18:00", "0 6 * * 5", 9, False),
    ("字典无 cron 回退固定阈值", "2026-11-01 06:06", "2026-11-11 12:00", None, 9, True),
    ("cron 非法回退固定阈值", "2026-11-01 06:06", "2026-11-11 12:00", "not a cron", 9, True),
    ("硬上限防长期静默", "2026-11-01 06:06", "2026-11-25 12:00", "0 6 * * 5", 9, True),
    ("日频沿用固定阈值", "2026-11-08 16:20", "2026-11-12 16:20", "0 16 * * *", 3, True),
    ("月频正常周期内不误报", "2026-11-01 06:30", "2026-11-10 18:00", "30 6 1 * *", 40, False),
]


def test_verdict_cases():
    for name, last_s, now_s, sch, gap, want in CASES:
        got, detail = job_heartbeat_verdict(
            _d(last_s), _d(now_s), schedule=sch, expected_gap_days=gap)
        assert got == want, f"{name}: got={got} want={want} detail={detail}"


def test_checker_is_wired_to_verdict_and_schedule_dict():
    import inspect  # noqa: PLC0415

    src = inspect.getsource(_check_job_heartbeat)
    assert "job_heartbeat_verdict" in src, "定义了判据却没接进巡检"
    assert "ops.scheduled_task" in src, "没读频率单一真相字典"


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _RoutingSession:
    """按 SQL 关键字分发：字典查询返回 schedule 行，其余返回 task_run 行。"""

    def __init__(self, task_run_rows, schedule_rows):
        self.task_run_rows = task_run_rows
        self.schedule_rows = schedule_rows

    def execute(self, query, params=None):
        if "ops.scheduled_task" in str(query):
            return _Result(self.schedule_rows)
        return _Result(self.task_run_rows)


class _FrozenDatetime(datetime):
    frozen = _d("2026-11-10 18:00")

    @classmethod
    def now(cls, tz=None):
        return cls.frozen if tz is None else cls.frozen.astimezone(tz)


def _end_to_end(monkeypatch, last, sched_rows, gap=9):
    monkeypatch.setattr(alerting, "datetime", _FrozenDatetime)
    monkeypatch.setattr(
        "app.scheduler_jobs.monitored_jobs",
        lambda: [JobSpec("lstm_weekly_retrain", "lstm_train", "weekly", gap, True, True)])
    session = _RoutingSession(
        [("lstm_train", "weekly", last)], sched_rows)
    return _check_job_heartbeat(session, AlertConfig())


def test_reschedule_gap_no_false_alarm_end_to_end(monkeypatch):
    """11-01 周日成功、现行 cron 周六、now=11-10：13 天合法空档不得告警。"""
    out = _end_to_end(monkeypatch, _d("2026-11-01 06:06"),
                      [("lstm_weekly_retrain", "0 6 * * 5")])
    assert out == [], f"改期过渡空档不应告警，实际: {out}"


def test_two_missed_fires_alarm_end_to_end(monkeypatch):
    """连续错过两次周六触发后必须告警。"""
    monkeypatch.setattr(alerting, "datetime",
                        type("F", (_FrozenDatetime,), {"frozen": _d("2026-11-15 12:00")}))
    monkeypatch.setattr(
        "app.scheduler_jobs.monitored_jobs",
        lambda: [JobSpec("lstm_weekly_retrain", "lstm_train", "weekly", 9, True, True)])
    session = _RoutingSession(
        [("lstm_train", "weekly", _d("2026-11-01 06:06"))],
        [("lstm_weekly_retrain", "0 6 * * 5")])
    out = _check_job_heartbeat(session, AlertConfig())
    assert len(out) == 1
    assert "lstm_train" in out[0]["msg"]


def test_missing_schedule_dict_falls_back(monkeypatch):
    """字典不可读/不匹配时回退固定阈值，停摆仍要能报出来。"""
    out = _end_to_end(monkeypatch, _d("2026-11-01 06:06"), [])
    assert len(out) == 1
    assert "固定阈值" in out[0]["msg"]
