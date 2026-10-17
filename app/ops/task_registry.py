# -*- coding: utf-8 -*-
"""定时任务字典与运行流水（2026-10-14 用户需求落地）。

背景
----
用户 2026-10-14 要求：把所有定时任务（数据库内跑的 TimescaleDB 作业、crontab
系统任务、APScheduler 作业、systemd 常驻服务）和存储过程统一登记到数据库字典表
``ops.scheduled_task``，后期定时任务通过字典表控制；每次运行的成败记录落
``ops.scheduled_task_run``，供查询对账。DDL 见 ``migrations/043_scheduled_task_registry.sql``。

本模块是代码侧唯一入口，提供四类能力：

1. **注册门** :func:`task_enabled` —— APScheduler 注册前查字典 ``enabled``；
   字典明确禁用则不注册（日志显式说明，绝不静默）。
2. **流水记录** :func:`record_run` / :func:`record_execution` —— 每次执行的
   start/finish/成败落 ``ops.scheduled_task_run``。
3. **存储过程执行记录** :func:`call_sp_recorded` / :func:`sp_enabled` ——
   CALL 存储过程同样过字典门并记流水（用户要求"存储过程也需要"）。
4. **APScheduler 监听** :func:`apply_registry_to_scheduler` —— 统一给调度器
   挂：① 字典禁用的作业移除；② 按字典 ``schedule`` 列（标准 5 段 cron）重注册作业
   触发时间，使 DB 成为频率单一真相、覆盖代码硬编码 cron；③ 全部作业的
   SUBMITTED/EXECUTED/ERROR/MISSED 事件落流水（不依赖各作业自己写 task_run）。
5. **周期再同步** :func:`resync_jobs_from_db` —— 由内置 ``scheduler_resync`` 作业
   定时调用，把 ``ops.scheduled_task`` 的频率/禁用变更**即时**应用到运行中的作业，
   免去 ``docker restart qhyc-api`` 才重载的限制（启动时的 ``apply_registry_to_scheduler``
   只跑一次）。已知限制：仅处理"已注册作业"，DB 新增/重新启用作业仍需重启。

防静默失效纪律（本项目头号缺陷类型）
----------------------------------
* 字典表不存在（迁移未应用）→ :func:`task_enabled` 按调用方默认值放行并
  **一次性 WARNING**；记录类调用跳过并 WARNING，绝不把作业搞挂。
* task_id 未登记 → 记录前**自动补一行最小字典行**（purpose='待补录'）再写
  流水，宁可留下显眼的"待补录"痕迹，也不让运行证据因外键静默丢失。
* 所有记录函数**任何异常只记日志不外抛**——记账失败不能拖垮生产作业。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import text

from app.core.logging import logger

#: 字典/流水不可用时的告警只发一次（避免每作业每次执行都刷屏）
_WARNED: set[str] = set()
_WARN_LOCK = threading.Lock()

#: 系统/运维类作业：永不被字典禁用逻辑移除（避免"自我关停"再同步等基础设施）。
#: 这些作业的"启用"由代码保证，但频率仍可由 DB schedule 调。
SYSTEM_JOBS: set[str] = {"scheduler_resync"}


def _warn_once(key: str, msg: str) -> None:
    with _WARN_LOCK:
        if key not in _WARNED:
            _WARNED.add(key)
            logger.warning(msg)


def _reset_warn_once() -> None:
    """测试用：重置"只警告一次"的去重状态。"""
    with _WARN_LOCK:
        _WARNED.clear()


# ---------------------------------------------------------------------------
# 字典读取（注册门）
# ---------------------------------------------------------------------------

def task_enabled(task_id: str, default: bool = True) -> bool:
    """字典总闸：返回该任务是否启用。

    语义（与迁移 043 注释 D2/D3 一致）：
      * 字典表不存在 / 查询失败 → 返回 ``default``（fail-open，保证不因记账
        基础设施故障全线停摆），并一次性 WARNING；
      * 行存在 → 返回其 ``enabled``；
      * 行不存在 → 返回 ``default``（未登记任务按代码既有行为跑），WARNING 一次。
    """
    from app.core.db import get_session_factory

    try:
        factory = get_session_factory()
        with factory() as s:
            row = s.execute(
                text("SELECT enabled FROM ops.scheduled_task WHERE task_id = :id"),
                {"id": task_id},
            ).scalar()
    except Exception as exc:  # noqa: BLE001 —— 表未建/库不可用都不该拖垮调用方
        _warn_once(
            "registry_unavailable",
            f"[task_registry] ops.scheduled_task 不可读（迁移 043 未应用？）：{exc}；"
            f"task_enabled 全部按代码默认放行",
        )
        return default

    if row is None:
        _warn_once(
            f"unregistered:{task_id}",
            f"[task_registry] task_id={task_id!r} 未登记于 ops.scheduled_task，"
            f"按代码默认 enabled={default} 执行；请跑 scripts/ops/sync_task_registry.py 补录",
        )
        return default
    return bool(row)


def disabled_apscheduler_jobs() -> set[str] | None:
    """返回字典中 enabled=false 的 apscheduler 作业 id 集合。

    表不可用返回 ``None``（调用方按"无禁用"处理）。
    """
    from app.core.db import get_session_factory

    try:
        factory = get_session_factory()
        with factory() as s:
            rows = s.execute(
                text(
                    "SELECT task_id FROM ops.scheduled_task "
                    "WHERE executor = 'apscheduler' AND enabled = false"
                )
            ).scalars()
        return set(rows)
    except Exception as exc:  # noqa: BLE001
        _warn_once(
            "registry_unavailable_disabled",
            f"[task_registry] 读取禁用清单失败（迁移 043 未应用？）：{exc}；不移除任何作业",
        )
        return None


# ---------------------------------------------------------------------------
# 流水记录
# ---------------------------------------------------------------------------

def _ensure_task_row(session: Any, task_id: str) -> None:
    """在**同一会话/事务**内确保字典行存在：缺则补一行最小行（fail-loud 留痕）。

    没有这一步，未登记 task_id 的流水会因外键直接插入失败——运行证据静默丢失，
    正是本项目头号缺陷类型。宁可留下显眼的"待补录"痕迹，也不丢证据。
    """
    exists = session.execute(
        text("SELECT 1 FROM ops.scheduled_task WHERE task_id = :id"),
        {"id": task_id},
    ).scalar()
    if exists:
        return
    session.execute(
        text(
            "INSERT INTO ops.scheduled_task "
            "(task_id, name, purpose, executor, schedule) "
            "VALUES (:id, :id, :purpose, 'manual', 'unknown') "
            "ON CONFLICT (task_id) DO NOTHING"
        ),
        {
            "id": task_id,
            "purpose": "待补录：由运行记录自动创建，请补全 name/purpose/schedule "
                       "并跑 scripts/ops/sync_task_registry.py",
        },
    )
    logger.warning(
        f"[task_registry] task_id={task_id!r} 未登记，已自动补最小字典行（purpose=待补录）；"
        f"请尽快补全登记信息"
    )


def record_run(
    task_id: str,
    status: str,
    *,
    trigger: str = "auto",
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    duration_s: float | None = None,
    exit_code: int | None = None,
    message: str | None = None,
    payload: dict | None = None,
    host: str | None = None,
) -> int | None:
    """写一条运行流水（一次性记录完整结果）。返回 run id；失败返回 None。

    绝不外抛：记账失败只记日志（生产作业成败不能被记账拖垮）。
    """
    from app.core.db import get_session_factory

    msg = (message or "")[:500] or None
    try:
        factory = get_session_factory()
        with factory() as s:
            _ensure_task_row(s, task_id)
            run_id = s.execute(
                text(
                    "INSERT INTO ops.scheduled_task_run "
                    "(task_id, status, trigger, started_at, finished_at, duration_s, "
                    " exit_code, message, payload, host) "
                    "VALUES (:task_id, :status, :trigger, :started_at, :finished_at, "
                    "        :duration_s, :exit_code, :message, "
                    "        CAST(:payload AS jsonb), :host) "
                    "RETURNING id"
                ),
                {
                    "task_id": task_id,
                    "status": status,
                    "trigger": trigger,
                    "started_at": started_at or datetime.now(timezone.utc),
                    "finished_at": finished_at or datetime.now(timezone.utc),
                    "duration_s": duration_s,
                    "exit_code": exit_code,
                    "message": msg,
                    "payload": _json_dumps(payload or {}),
                    "host": host,
                },
            ).scalar()
            s.commit()
        return int(run_id) if run_id is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[task_registry] record_run 失败 task_id={task_id}: {exc}")
        return None


def record_start(
    task_id: str,
    *,
    trigger: str = "auto",
    payload: dict | None = None,
    host: str | None = None,
) -> int | None:
    """插入一条 running 流水，返回 run id（供 :func:`record_finish` 更新）。"""
    from app.core.db import get_session_factory

    try:
        factory = get_session_factory()
        with factory() as s:
            _ensure_task_row(s, task_id)
            run_id = s.execute(
                text(
                    "INSERT INTO ops.scheduled_task_run "
                    "(task_id, status, trigger, started_at, payload, host) "
                    "VALUES (:task_id, 'running', :trigger, now(), "
                    "        CAST(:payload AS jsonb), :host) RETURNING id"
                ),
                {
                    "task_id": task_id,
                    "trigger": trigger,
                    "payload": _json_dumps(payload or {}),
                    "host": host,
                },
            ).scalar()
            s.commit()
        return int(run_id) if run_id is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[task_registry] record_start 失败 task_id={task_id}: {exc}")
        return None


def record_finish(
    run_id: int | None,
    status: str,
    *,
    message: str | None = None,
    exit_code: int | None = None,
    payload: dict | None = None,
) -> None:
    """把 running 流水更新为终态。绝不外抛。"""
    if run_id is None:
        return
    from app.core.db import get_session_factory

    msg = (message or "")[:500] or None
    try:
        factory = get_session_factory()
        with factory() as s:
            s.execute(
                text(
                    "UPDATE ops.scheduled_task_run SET "
                    "status = :status, finished_at = now(), "
                    "duration_s = EXTRACT(EPOCH FROM (now() - started_at)), "
                    "exit_code = :exit_code, message = COALESCE(:message, message), "
                    "payload = payload || CAST(:payload AS jsonb) "
                    "WHERE id = :id"
                ),
                {
                    "id": run_id,
                    "status": status,
                    "message": msg,
                    "exit_code": exit_code,
                    "payload": _json_dumps(payload or {}),
                },
            )
            s.commit()
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[task_registry] record_finish 失败 run_id={run_id}: {exc}")


@contextmanager
def record_execution(
    task_id: str,
    *,
    trigger: str = "auto",
    payload: dict | None = None,
    host: str | None = None,
    ensure_registered: bool = True,
) -> Iterator[None]:
    """包住一段执行：进入插 running 行，正常退出记 success，异常记 failed 并重抛。

    ``record_start`` 内部会自动确保字典行存在（缺行补"待补录"最小行），
    ``ensure_registered`` 参数保留仅为兼容旧调用签名，无额外行为。

    用法::

        with record_execution("sp_build_l2_back_adjusted", payload={"freq": f}):
            s.execute(text("CALL l2_adj.sp_build_l2_back_adjusted(:f)"), {"f": f})
    """
    run_id = record_start(task_id, trigger=trigger, payload=payload, host=host)
    try:
        yield
    except Exception as exc:  # noqa: BLE001 —— 记账后原样抛出，不掩盖真实错误
        record_finish(run_id, "failed", message=_exc_tail(exc))
        raise
    record_finish(run_id, "success")


def call_sp_recorded(
    session: Any,
    sql: str,
    params: dict | None = None,
    *,
    task_id: str,
    payload: dict | None = None,
) -> None:
    """在调用方 session 上执行存储过程 SQL，并把成败记入流水。

    与 :func:`record_execution` 的区别：SQL 由调用方给出（含 CALL 语句），
    执行仍在调用方事务内（失败随事务回滚），但流水写独立 session——失败证据
    不随回滚丢失。异常原样抛出。
    """
    with record_execution(task_id, payload=payload or {"sql": sql[:200]}):
        session.execute(text(sql), params or {})


def sp_enabled(task_id: str, default: bool = True) -> bool:
    """存储过程的字典门（与 :func:`task_enabled` 同源，单独命名强调用途）。"""
    return task_enabled(task_id, default=default)


# ---------------------------------------------------------------------------
# APScheduler 集成
# ---------------------------------------------------------------------------

def _parse_db_cron(expr: Any, tz: Any) -> Any | None:
    """把 ``ops.scheduled_task.schedule`` 的 5 段 cron 字符串解析为 CronTrigger。

    仅当字符串为合法 cron（恰好 5 字段、可被 APScheduler 解析）时返回 trigger；
    否则返回 ``None``（调用方沿用代码内硬编码的默认 cron，绝不抛错）。这样即使
    schedule 列仍是早期"目录说明"文本也不会破坏调度。
    """
    if expr is None:
        return None
    expr = str(expr).strip()
    if not expr:
        return None
    # 合法 cron 必须恰好 5 字段（分 时 日 月 周）；否则视为说明文本，忽略
    if len(expr.split()) != 5:
        return None
    try:
        from apscheduler.triggers.cron import CronTrigger

        return CronTrigger.from_crontab(expr, timezone=tz)
    except Exception as exc:  # noqa: BLE001
        _warn_once(
            f"bad_schedule:{expr}",
            f"[task_registry] schedule 列值 {expr!r} 不是合法 cron（已忽略，沿用代码默认）: {exc}",
        )
        return None


def _reregister_schedules_from_db(sched: Any) -> None:
    """按字典 ``schedule`` 列重注册 apscheduler 作业的触发时间。

    在 :func:`apply_registry_to_scheduler` 内、禁用作业移除之后调用：读取
    ``ops.scheduled_task(executor='apscheduler')`` 的 schedule 列，对当前已注册的作业
    用 ``CronTrigger.from_crontab`` 重建触发器（``sched.reschedule_job`` 保留
    func/args/kwargs/max_instances/coalesce 等，仅换触发时间）。合法的 schedule 即视为
    频率的"单一真相"，覆盖 ``_build_scheduler`` 里的硬编码 ``CronTrigger``；非法/空则
    保留代码默认。任何异常只记日志，不影响调度器启动。
    """
    tz = getattr(sched, "timezone", None)
    try:
        from app.core.db import get_session_factory

        factory = get_session_factory()
        with factory() as s:
            rows = s.execute(
                text(
                    "SELECT task_id, schedule FROM ops.scheduled_task "
                    "WHERE executor = 'apscheduler'"
                )
            ).all()
    except Exception as exc:  # noqa: BLE001
        _warn_once(
            "registry_unavailable_schedule",
            f"[task_registry] 读取 schedule 列失败（沿用代码默认 cron）: {exc}",
        )
        return

    db_schedule = {str(r[0]): ("" if r[1] is None else str(r[1])) for r in rows}
    for job in sched.get_jobs():
        expr = db_schedule.get(job.id)
        if not expr:  # 未登记 schedule 的作业：保留代码默认
            continue
        trigger = _parse_db_cron(expr, tz)
        if trigger is None:
            continue
        try:
            sched.reschedule_job(job.id, trigger=trigger)
            logger.info(
                f"[task_registry] 作业 {job.id} 触发时间已按字典 schedule={expr!r} 重注册"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[task_registry] 作业 {job.id} 按 schedule 重注册失败（沿用默认）: {exc}"
            )


def _job_crontab(trigger: Any) -> str | None:
    """返回触发器的稳定签名串，用于与 DB 重算结果做幂等比较。

    直接复用 ``CronTrigger.__repr__``（含 minute/hour/day/month/day_of_week 与
    timezone，字段顺序固定）：两个**同源解析**出的 trigger 其 repr 必相等 → 已同步则
    跳过、不抖动；非 CronTrigger（如 IntervalTrigger）返回 None。
    """
    try:
        from apscheduler.triggers.cron import CronTrigger
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(trigger, CronTrigger):
        return None
    try:
        return repr(trigger)
    except Exception:  # noqa: BLE001
        return None


def resync_jobs_from_db(sched: Any) -> None:
    """周期再同步：把 DB 的 schedule/enabled 变更应用到运行中的作业，免重启。

    与 :func:`apply_registry_to_scheduler`（仅启动时跑一次）互补：本函数由内置的
    ``scheduler_resync`` 作业定时调用，使 ``ops.scheduled_task`` 的频率/禁用变更
    **即时生效**，无需 ``docker restart qhyc-api``：

      * schedule 列（合法 5 段 cron）与当前触发时间不一致 → ``reschedule_job``；
      * enabled=false 且作业当前在跑 → ``remove_job``（基础设施作业除外，见
        :data:`SYSTEM_JOBS`，防止"自我关停"再同步）；
      * 已按 DB 同步的作业不做任何动作（幂等，无抖动）。

    已知限制：DB 新增/重新启用的作业（需重建 func/args，字典表未存）不在本函数范围，
    仍须重启；本函数只处理"已注册作业的触发时间 + 禁用开关"。
    任何异常只记日志，绝不拖垮调度器。
    """
    tz = getattr(sched, "timezone", None)
    try:
        from app.core.db import get_session_factory

        factory = get_session_factory()
        with factory() as s:
            rows = s.execute(
                text(
                    "SELECT task_id, schedule, enabled FROM ops.scheduled_task "
                    "WHERE executor = 'apscheduler'"
                )
            ).all()
    except Exception as exc:  # noqa: BLE001
        _warn_once(
            "registry_unavailable_resync",
            f"[task_registry] 周期再同步读取失败（跳过本次）: {exc}",
        )
        return

    db = {
        str(r[0]): {
            "schedule": ("" if r[1] is None else str(r[1])),
            "enabled": bool(r[2]),
        }
        for r in rows
    }

    for job in sched.get_jobs():
        rec = db.get(job.id)
        if rec is None:
            continue  # 纯代码作业（不在字典）：不动，交给代码默认
        # ① 禁用：移除作业（基础设施作业除外，防自我关停）
        if not rec["enabled"] and job.id not in SYSTEM_JOBS:
            try:
                sched.remove_job(job.id)
                logger.info(
                    f"[task_registry] 周期再同步：作业 {job.id} 字典已禁用，已移除"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"[task_registry] 周期再同步移除作业 {job.id} 失败: {exc}"
                )
            continue
        # ② 频率：仅当与当前触发时间不一致才 reschedule（幂等，无抖动）
        expr = rec["schedule"]
        if not expr:
            continue
        trigger = _parse_db_cron(expr, tz)
        if trigger is None:
            continue
        try:
            cur = _job_crontab(job.trigger)
            new = _job_crontab(trigger)
        except Exception:  # noqa: BLE001
            cur = new = None
        if cur is not None and cur == new:
            continue  # 已同步，跳过
        try:
            sched.reschedule_job(job.id, trigger=trigger)
            logger.info(
                f"[task_registry] 周期再同步：作业 {job.id} 触发时间已更新为 "
                f"schedule={expr!r}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[task_registry] 周期再同步作业 {job.id} 失败（沿用当前）: {exc}"
            )


def apply_registry_to_scheduler(sched: Any) -> None:
    """给调度器应用字典控制：① 移除 enabled=false 的作业；② 按 schedule 列重注册
    触发时间；③ 挂运行流水监听。

    在 ``_build_scheduler`` 返回前调用（scheduler.py 与 pipeline/worker.py 均已接）。
    任何失败只记日志，不影响调度器启动。
    """
    try:
        disabled = disabled_apscheduler_jobs()
        if disabled:
            for job in sched.get_jobs():
                if job.id in disabled and job.id not in SYSTEM_JOBS:
                    try:
                        sched.remove_job(job.id)
                        logger.info(
                            f"[task_registry] 作业 {job.id} 被字典禁用"
                            f"（ops.scheduled_task.enabled=false），已移除不注册"
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"[task_registry] 移除作业 {job.id} 失败: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[task_registry] 字典门应用失败（按原样注册）: {exc}")

    # ② 按字典 schedule 列重注册触发时间（DB 成为频率单一真相，覆盖硬编码 cron）
    try:
        _reregister_schedules_from_db(sched)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[task_registry] schedule 重注册失败（沿用代码默认 cron）: {exc}")

    install_run_recorder(sched)


def install_run_recorder(sched: Any) -> bool:
    """挂 APScheduler 事件监听：全部作业的成败落 ops.scheduled_task_run。

    事件配对：SUBMITTED 插 running 行 → EXECUTED/ERROR 更新为终态；
    MISSED 直接插一条 missed 行（无 SUBMITTED）。绝不外抛。
    """
    try:
        from apscheduler.events import (
            EVENT_JOB_ERROR,
            EVENT_JOB_EXECUTED,
            EVENT_JOB_MISSED,
            EVENT_JOB_SUBMITTED,
        )
    except Exception as exc:  # noqa: BLE001 —— apscheduler 版本不符就放弃监听
        logger.warning(f"[task_registry] apscheduler 事件导入失败，运行流水监听未安装: {exc}")
        return False

    # job_id -> run_id（SUBMITTED 与 EXECUTED/ERROR 配对）
    pending: dict[str, int] = {}

    def _on_submitted(event: Any) -> None:
        job_id = str(getattr(event, "job_id", ""))
        if not job_id:
            return
        sched_times = getattr(event, "scheduled_run_times", None) or []
        payload: dict[str, Any] = {}
        if sched_times:
            try:
                payload["scheduled_run_time"] = sched_times[0].isoformat()
            except Exception:  # noqa: BLE001
                pass
        run_id = record_start(job_id, trigger="auto", payload=payload)
        if run_id is not None:
            pending[job_id] = run_id

    def _on_done(event: Any, status: str) -> None:
        job_id = str(getattr(event, "job_id", ""))
        run_id = pending.pop(job_id, None)
        if status == "success":
            retval = getattr(event, "retval", None)
            message = _truncate(repr(retval))
        else:
            exc = getattr(event, "exception", None)
            tb = getattr(event, "traceback", None)
            message = _truncate(f"{type(exc).__name__}: {exc}" if exc else str(tb or ""))
        record_finish(run_id, status, message=message)

    def _on_error(event: Any) -> None:
        _on_done(event, "failed")

    def _on_executed(event: Any) -> None:
        _on_done(event, "success")

    def _on_missed(event: Any) -> None:
        job_id = str(getattr(event, "job_id", ""))
        if not job_id:
            return
        sched_time = getattr(event, "scheduled_run_time", None)
        record_run(
            job_id,
            "missed",
            trigger="auto",
            started_at=_as_utc(sched_time),
            message="错过调度窗口（misfire_grace_time 内未执行）",
            payload={"scheduled_run_time": _iso(sched_time)},
        )

    try:
        sched.add_listener(_on_submitted, EVENT_JOB_SUBMITTED)
        sched.add_listener(_on_executed, EVENT_JOB_EXECUTED)
        sched.add_listener(_on_error, EVENT_JOB_ERROR)
        sched.add_listener(_on_missed, EVENT_JOB_MISSED)
        logger.info("[task_registry] 运行流水监听已安装（SUBMITTED/EXECUTED/ERROR/MISSED）")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[task_registry] 运行流水监听安装失败: {exc}")
        return False


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _json_dumps(obj: Any) -> str:
    import json

    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return "{}"


def _truncate(s: str, n: int = 500) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 3] + "..."


def _exc_tail(exc: BaseException, n: int = 500) -> str:
    return _truncate(f"{type(exc).__name__}: {exc}", n)


def _as_utc(dt: Any) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    try:
        if getattr(dt, "tzinfo", None) is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc)


def _iso(dt: Any) -> str | None:
    try:
        return dt.isoformat() if dt is not None else None
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "task_enabled",
    "sp_enabled",
    "disabled_apscheduler_jobs",
    "record_run",
    "record_start",
    "record_finish",
    "record_execution",
    "call_sp_recorded",
    "apply_registry_to_scheduler",
    "install_run_recorder",
    "resync_jobs_from_db",
]
