"""M8 决策链路常驻 worker（`python -m app.pipeline.worker`）。

- 启动时做一次源码快照（copy-on-start，PRD §5.3）并把 md5 摘要落库
- 注册 APScheduler cron：日链 17:40 / 整点 signal（signal 前串行 intraday）/ 周一 08:05 自检
- 每步用 **subprocess** 调 `run_pipeline.py`（§5.4），不做 import
- 运行前可选 readiness 闸门；失败/超时 → status=fail|partial + pushplus 告警

⚠ 不改任何策略口径；本模块只负责编排、留痕与告警。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.logging import logger
from app.pipeline import config, readiness, recorder, snapshots

_KIND_ARGS = {
    "daily": ["--daily"],
    "signal": ["--signal"],
    "intraday": ["--intraday"],
    "check": ["--check"],
}


def ensure_snapshot() -> tuple[str, str]:
    """确保快照存在，返回 (快照目录, manifest 摘要)。缺失则现场同步。"""
    cfg = config.load()
    dest = Path(cfg.dest)
    if not dest.exists() or not any(dest.iterdir()):
        snapshots.sync(cfg.src, cfg.dest)
    digest = snapshots.manifest_digest(snapshots.manifest_of(cfg.dest))
    return str(dest), digest


def _collect_artifacts(base: str, since_ts: float) -> list[str]:
    """列出本轮运行后产出/更新的文件（限 30 个）。"""
    try:
        root = Path(base)
        if not root.exists():
            return []
        hits = []
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if "__pycache__" in p.parts:
                continue
            try:
                if p.stat().st_mtime >= since_ts - 5:
                    hits.append(str(p))
            except OSError:
                continue
        return sorted(hits)[:30]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[pipeline] 收集产物失败: {e}")
        return []


def _notify(title: str, content: str) -> None:
    try:
        from app.notify.pushplus import send_notify

        ok, channel = send_notify(title, content)
        logger.info(f"[pipeline] 告警推送 ok={ok} channel={channel}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[pipeline] 告警推送失败: {e}")


def run_once(kind: str, *, wait_readiness: bool = False,
             extra_args: list[str] | None = None) -> dict:
    """执行一次链路（供 cron 与 API 手动触发复用）。"""
    cfg = config.load()
    snapshot, digest = ensure_snapshot()
    data_ready: bool | None = None
    if wait_readiness and cfg.readiness_enabled:
        data_ready = readiness.wait_ready(cfg.readiness_timeout_min, cfg.readiness_poll_sec)
        if not data_ready and cfg.notify_on_failure:
            _notify("⚠ 决策链路降级：数据未就绪",
                    f"run_date={date.today()} readiness 超时 {cfg.readiness_timeout_min} 分钟，"
                    f"轻基本面相关字段将标 N/A，链路照常继续。")

    run_id = recorder.start_run(kind, date.today(), digest)
    args = list(_KIND_ARGS.get(kind, ["--daily"])) + list(extra_args or [])
    env = os.environ.copy()
    env.update({
        "QH_BRIEF_BASE": cfg.brief_base,
        "PYTHONPATH": snapshot,
        "PYTHONDONTWRITEBYTECODE": "1",
        "TZ": os.environ.get("TZ", "Asia/Shanghai"),
    })
    t0 = time.time()
    status, error, code = "ok", None, 0
    try:
        proc = subprocess.run(
            [sys.executable, "run_pipeline.py", *args],
            cwd=snapshot, env=env, capture_output=True, text=True,
            timeout=cfg.step_timeout_sec,
        )
        code = proc.returncode
        status = "ok" if code == 0 else "fail"
        if code != 0:
            error = (proc.stderr or "")[-1500:]
        logger.info(f"[pipeline] {kind} exit={code} out_tail={(proc.stdout or '')[-300:]}")
    except subprocess.TimeoutExpired:
        status, error = "partial", f"timeout>{cfg.step_timeout_sec}s"
    except Exception as e:  # noqa: BLE001
        status, error = "fail", str(e)[:500]

    steps = [{
        "i": 1, "name": kind, "ok": status == "ok",
        "dur_s": round(time.time() - t0, 1), "exit": code,
    }]
    artifacts = _collect_artifacts(cfg.brief_base, t0)
    recorder.finish_run(run_id, status, steps=steps, artifacts=artifacts,
                        data_ready=data_ready, error=error)
    if status != "ok" and cfg.notify_on_failure:
        _notify(f"❌ 决策链路 {kind} 失败",
                f"run_id={run_id} status={status}\n{error or ''}"[:1500])
    return {"run_id": run_id, "status": status, "data_ready": data_ready,
            "artifacts": artifacts, "error": error}


def _daily_job() -> None:
    try:
        run_once("daily", wait_readiness=True)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[pipeline] daily job error: {e}")


def _signal_job() -> None:
    """整点：先盘中监控（串行），再信号推送。"""
    cfg = config.load()
    try:
        if cfg.intraday_enabled:
            run_once("intraday")
        run_once("signal")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[pipeline] signal job error: {e}")


def _check_job() -> None:
    try:
        run_once("check")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[pipeline] check job error: {e}")


def build_scheduler() -> BlockingScheduler:
    cfg = config.load()
    from app.core.config import get_settings

    sched = BlockingScheduler(timezone=get_settings().env.TZ or "Asia/Shanghai")
    if not cfg.enabled:
        logger.warning("[pipeline] pipeline.enabled=false：仍启动但只注册自检，便于手动触发")
    sched.add_job(
        _daily_job,
        trigger=CronTrigger(hour=cfg.daily_hour, minute=cfg.daily_minute,
                            timezone=get_settings().env.TZ),
        id="pipeline_daily", replace_existing=True,
        max_instances=cfg.max_instances, coalesce=True,
        misfire_grace_time=600,
    )
    logger.info(f"[pipeline] registered daily {cfg.daily_hour:02d}:{cfg.daily_minute:02d}")

    for slot in cfg.signal_slots:
        try:
            hh, mm = str(slot).split(":")[:2]
            sched.add_job(
                _signal_job,
                trigger=CronTrigger(hour=int(hh), minute=int(mm),
                                    timezone=get_settings().env.TZ),
                id=f"pipeline_signal_{hh}{mm}", replace_existing=True,
                max_instances=cfg.max_instances, coalesce=True,
                misfire_grace_time=300,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[pipeline] 无效 signal slot {slot}: {e}")
    logger.info(f"[pipeline] registered signal slots {cfg.signal_slots}")

    sched.add_job(
        _check_job,
        trigger=CronTrigger(day_of_week=cfg.check_day, hour=cfg.check_hour,
                            minute=cfg.check_minute, timezone=get_settings().env.TZ),
        id="pipeline_check", replace_existing=True,
        max_instances=1, coalesce=True, misfire_grace_time=600,
    )
    logger.info(f"[pipeline] registered check {cfg.check_day} "
                f"{cfg.check_hour:02d}:{cfg.check_minute:02d}")
    return sched


def main() -> None:
    cfg = config.load()
    recorder.ensure_tables()   # db/init 只在空卷首跑，这里兜底建表（幂等）
    logger.info(f"[pipeline] worker start src={cfg.src} dest={cfg.dest} base={cfg.brief_base}")
    snap, digest = ensure_snapshot()
    logger.info(f"[pipeline] snapshot ready: {snap} digest={digest}")
    sched = build_scheduler()
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("[pipeline] worker stopped")


if __name__ == "__main__":
    main()
