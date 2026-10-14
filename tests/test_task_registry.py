"""app/ops/task_registry 契约测试（2026-10-14，迁移 043 配套）。

覆盖四类防静默失效纪律（本项目头号缺陷类型）：
  1. 字典表不可读 → task_enabled 按 default 放行且绝不抛错；
  2. task_id 未登记 → task_enabled 按 default 放行（WARNING 一次）；
  3. 流水记录前**同事务自动补字典行**——未登记任务的运行证据不得因外键丢失；
  4. record_execution 异常记 failed 并**原样重抛**（不掩盖真实错误）；
  5. 字典禁用的 apscheduler 作业被移除；运行流水监听装上 4 类事件。

全 fake 内存库，不需要真实 Postgres（对齐 tests/test_scheduler_switch.py 的注入法）。
"""
from __future__ import annotations

import pytest
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.ops import task_registry as tr


# -----------------------------------------------------------
# 内存 fake（模拟 ops 两张表的最小行为）
# -----------------------------------------------------------
class _Result:
    def __init__(self, scalar=None, scalars=None):
        self._scalar = scalar
        self._scalars = scalars or []

    def scalar(self):
        return self._scalar

    def scalars(self):
        return iter(self._scalars)


class _FakeSession:
    def __init__(self, store: dict):
        self.store = store  # {"tasks": {...}, "runs": [...], "raise_on": set()}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        pass

    def execute(self, stmt, params=None):
        sql = str(stmt)
        p = params or {}
        tasks = self.store["tasks"]
        runs = self.store["runs"]

        if "SELECT enabled FROM ops.scheduled_task" in sql:
            row = tasks.get(p.get("id"))
            return _Result(scalar=None if row is None else row["enabled"])
        if "SELECT 1 FROM ops.scheduled_task" in sql:
            return _Result(scalar=1 if p.get("id") in tasks else None)
        if "SELECT task_id FROM ops.scheduled_task" in sql and "enabled = false" in sql:
            return _Result(scalars=[k for k, v in tasks.items()
                                    if v.get("executor") == "apscheduler"
                                    and v.get("enabled") is False])
        if "INSERT INTO ops.scheduled_task " in sql:
            tasks[p["id"]] = {
                "enabled": True, "executor": p.get("executor"),
                "purpose": p.get("purpose", ""),
            }
            return _Result()
        if "INSERT INTO ops.scheduled_task_run" in sql and "RETURNING id" in sql:
            run = {"id": len(runs) + 1, "task_id": p["task_id"], "status": p.get("status", "running")}
            runs.append(run)
            return _Result(scalar=run["id"])
        if "UPDATE ops.scheduled_task_run" in sql:
            for r in runs:
                if r["id"] == p.get("id"):
                    r["status"] = p["status"]
                    r["message"] = p.get("message")
            return _Result()
        raise AssertionError(f"fake 未覆盖的 SQL: {sql[:120]}")


class _FakeFactory:
    def __init__(self, store: dict):
        self.store = store

    def __call__(self):
        return _FakeSession(self.store)


@pytest.fixture
def store():
    return {"tasks": {}, "runs": []}


@pytest.fixture(autouse=True)
def _patch(monkeypatch, store):
    monkeypatch.setattr("app.core.db.get_session_factory", lambda: _FakeFactory(store))
    tr._reset_warn_once()


# -----------------------------------------------------------
# task_enabled
# -----------------------------------------------------------
def test_enabled_true_from_dict(store):
    store["tasks"]["t1"] = {"enabled": True, "executor": "apscheduler"}
    assert tr.task_enabled("t1", default=False) is True


def test_enabled_false_from_dict(store):
    store["tasks"]["t1"] = {"enabled": False, "executor": "apscheduler"}
    assert tr.task_enabled("t1", default=True) is False


def test_unregistered_falls_back_to_default(store):
    assert tr.task_enabled("nope", default=True) is True
    assert tr.task_enabled("nope", default=False) is False


def test_table_unavailable_never_raises(monkeypatch):
    def _boom():
        raise RuntimeError("relation ops.scheduled_task does not exist")

    monkeypatch.setattr("app.core.db.get_session_factory", _boom)
    # fail-open：表不可读按调用方 default 放行，且绝不抛错
    assert tr.task_enabled("t1", default=False) is False
    assert tr.task_enabled("t1", default=True) is True


def test_disabled_apscheduler_jobs(store):
    store["tasks"].update({
        "a": {"enabled": False, "executor": "apscheduler"},
        "b": {"enabled": True, "executor": "apscheduler"},
        "c": {"enabled": False, "executor": "crontab"},   # 非 apscheduler 不算注册门
    })
    assert tr.disabled_apscheduler_jobs() == {"a"}


# -----------------------------------------------------------
# 流水记录：自动补字典行（FK 防丢证据）
# -----------------------------------------------------------
def test_record_start_autocreates_task_row(store):
    run_id = tr.record_start("brand_new_job", payload={"k": 1})
    assert run_id == 1
    assert "brand_new_job" in store["tasks"]           # 自动补行
    assert "待补录" in store["tasks"]["brand_new_job"]["purpose"]
    assert store["runs"][0]["status"] == "running"


def test_record_run_full(store):
    store["tasks"]["t"] = {"enabled": True}
    tr.record_run("t", "success", exit_code=0, message="ok")
    assert len(store["runs"]) == 1
    assert store["runs"][0]["status"] == "success"


def test_record_finish_updates_status(store):
    store["tasks"]["t"] = {"enabled": True}
    rid = tr.record_start("t")
    tr.record_finish(rid, "failed", message="boom")
    assert store["runs"][0]["status"] == "failed"
    assert store["runs"][0]["message"] == "boom"


def test_record_finish_none_run_id_noop(store):
    tr.record_finish(None, "success")   # 不得抛错
    assert store["runs"] == []


def test_record_start_db_failure_returns_none(monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("app.core.db.get_session_factory", _boom)
    assert tr.record_start("t") is None   # 记账失败绝不外抛


# -----------------------------------------------------------
# record_execution
# -----------------------------------------------------------
def test_record_execution_success(store):
    store["tasks"]["sp_x"] = {"enabled": True}
    with tr.record_execution("sp_x"):
        pass
    assert store["runs"][0]["status"] == "success"


def test_record_execution_failed_reraises(store):
    store["tasks"]["sp_x"] = {"enabled": True}
    with pytest.raises(ValueError, match="boom"):
        with tr.record_execution("sp_x"):
            raise ValueError("boom")
    assert store["runs"][0]["status"] == "failed"
    assert "boom" in store["runs"][0]["message"]


# -----------------------------------------------------------
# APScheduler 集成
# -----------------------------------------------------------
def test_apply_registry_removes_disabled_jobs(store):
    store["tasks"].update({
        "job_on":  {"enabled": True, "executor": "apscheduler"},
        "job_off": {"enabled": False, "executor": "apscheduler"},
    })
    sched = BlockingScheduler()
    sched.add_job(lambda: None, trigger=IntervalTrigger(seconds=60), id="job_on")
    sched.add_job(lambda: None, trigger=IntervalTrigger(seconds=60), id="job_off")
    tr.apply_registry_to_scheduler(sched)
    ids = {j.id for j in sched.get_jobs()}
    assert ids == {"job_on"}


def test_apply_registry_table_unavailable_keeps_all(monkeypatch):
    def _boom():
        raise RuntimeError("no table")

    monkeypatch.setattr("app.core.db.get_session_factory", _boom)
    sched = BlockingScheduler()
    sched.add_job(lambda: None, trigger=IntervalTrigger(seconds=60), id="job_any")
    tr.apply_registry_to_scheduler(sched)   # 不得抛错
    assert {j.id for j in sched.get_jobs()} == {"job_any"}


def test_install_run_recorder_registers_four_listeners():
    sched = BlockingScheduler()
    assert tr.install_run_recorder(sched) is True
    assert len(sched._listeners) == 4
