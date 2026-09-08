"""
APScheduler 调度（M1，§4.2）
- 每日三次：12:00 / 16:00 / 08:00（次日，含夜盘）
- 触发后：
  1. 采集 + 校准（akshare → tqsdk 补缺 + 比对）
  2. 入库完成后 → M1 阶段占位（预测待 M2 接入）
- 任务状态写入 task_run 表
"""
from __future__ import annotations

import os
import signal
import sys
import time

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.logging import logger, setup_logging
from app.ingest.orchestrator import IngestOrchestrator
from app.repositories.task_repo import TaskRepository


def _predict_job(session, symbols: list[str], label: str) -> int:
    """⑦ 数据更新入库后立即触发预测（轻模型在线更新）"""
    from app.engine.service import predict_symbols

    repo = TaskRepository(session)
    run = repo.start("predict", label=label, payload={"symbols": symbols})
    try:
        out = predict_symbols(session, symbols=symbols)
        ok = sum(1 for r in out if "error" not in r)
        repo.finish(run, "success", f"{ok}/{len(out)} symbols")
        logger.info(f"[scheduler] predict done label={label} {ok}/{len(out)}")
        return ok
    except Exception as e:
        repo.finish(run, "failed", str(e))
        logger.exception(f"[scheduler] predict failed label={label}: {e}")
        return 0


def _ingest_job(label: str) -> None:
    """单次完整 ingest 任务（akshare + tqsdk 校准）→ 成功后联动预测（⑦）"""
    logger.info(f"[scheduler] ingest job start label={label}")
    try:
        with session_scope() as s:
            repo = TaskRepository(s)
            run = repo.start("ingest", label=label, payload={"trigger": "scheduler"})
            orch = IngestOrchestrator(s)
            reports = orch.ingest_all()
            failed = [r for r in reports if r.error]
            status = "success" if not failed else "partial"
            repo.finish(
                run,
                status,
                f"{len(reports)} symbols, failed={len(failed)}",
            )
            logger.info(
                f"[scheduler] ingest done label={label} status={status} "
                f"total={len(reports)} failed={len(failed)}"
            )
            # §16 大类指数更新（M2 排期）：分类同步 → 指数合成（增量重算）
            try:
                from app.sectors.builder import build_sector_index, sync_sector_map

                sync_sector_map(s)
                idx_stats = build_sector_index(s)
                logger.info(f"[scheduler] sector index done label={label}: {idx_stats}")
            except Exception as se:
                logger.warning(f"[scheduler] sector index failed label={label}: {se}")

            # M2.1 平滑主连自动延伸（R1）：先延伸再预测，保证预测用最新权威口径
            try:
                from app.ingest.smooth_extender import extend_all

                ext_results = extend_all(s)
                logger.info(f"[scheduler] smooth extend done label={label}: {ext_results}")
            except Exception as ee:
                logger.warning(f"[scheduler] smooth extend failed label={label}: {ee}")

            # ⑦ 预测联动：对本次成功入库的品种触发一次预测
            done_syms = [r.symbol for r in reports if not r.error]
            if done_syms:
                _predict_job(s, done_syms, label=label)
    except Exception as e:
        logger.exception(f"[scheduler] ingest job failed label={label}: {e}")


def _build_scheduler() -> BlockingScheduler:
    settings = get_settings()
    sched = BlockingScheduler(timezone=settings.env.TZ)
    for spec in settings.cron_specs:
        trigger = CronTrigger(
            hour=spec.hour, minute=spec.minute, timezone=settings.env.TZ
        )
        sched.add_job(
            _ingest_job,
            trigger=trigger,
            args=[spec.label],
            id=f"ingest_{spec.label}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            f"[scheduler] registered cron {spec.hour:02d}:{spec.minute:02d} ({spec.label})"
        )

    # ⑱ LSTM 每周重训一次（周六 06:00，避开交易时段）
    sched.add_job(
        _lstm_weekly_job,
        trigger=CronTrigger(day_of_week="sat", hour=6, minute=0, timezone=settings.env.TZ),
        id="lstm_weekly_retrain",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("[scheduler] registered cron sat 06:00 (lstm_weekly_retrain)")

    # §16.7 ⑤ 传导权重每周重算（与 LSTM 周训对齐，周六 06:30 在 LSTM 重训后）
    sched.add_job(
        _transmission_weekly_job,
        trigger=CronTrigger(day_of_week="sat", hour=6, minute=30, timezone=settings.env.TZ),
        id="transmission_weekly_recalc",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("[scheduler] registered cron sat 06:30 (transmission_weekly_recalc)")

    # M4 回测每周一次（周六 07:00，动态权重/传导更新后）
    sched.add_job(
        _backtest_weekly_job,
        trigger=CronTrigger(day_of_week="sat", hour=7, minute=0, timezone=settings.env.TZ),
        id="backtest_weekly",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("[scheduler] registered cron sat 07:00 (backtest_weekly)")

    # ⑳ 权重月更（每月 1 日 06:30，读最近回测的近 60 日准确率）
    sched.add_job(
        _weights_monthly_job,
        trigger=CronTrigger(day=1, hour=6, minute=30, timezone=settings.env.TZ),
        id="weights_monthly_update",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("[scheduler] registered cron day1 06:30 (weights_monthly_update)")
    return sched


def _backtest_weekly_job() -> None:
    """M4 周度回测：全品种逐评估点预测 vs 真实"""
    logger.info("[scheduler] weekly backtest start")
    try:
        with session_scope() as s:
            repo = TaskRepository(s)
            run = repo.start("backtest", label="weekly")
            try:
                from app.backtest.engine import BacktestParams, backtest_symbols

                out = backtest_symbols(s, params=BacktestParams())
                ok = sum(1 for r in out if "error" not in r and "skipped" not in r)
                repo.finish(run, "success", f"{ok}/{len(out)} symbols")
                logger.info(f"[scheduler] weekly backtest done {ok}/{len(out)}")
            except Exception as e:
                repo.finish(run, "failed", str(e))
                logger.exception(f"[scheduler] weekly backtest failed: {e}")
    except Exception as e:
        logger.exception(f"[scheduler] backtest weekly job error: {e}")


def _weights_monthly_job() -> None:
    """⑳ 权重月更：按最近回测的近 60 日准确率更新 model_weights"""
    logger.info("[scheduler] monthly weight update start")
    try:
        with session_scope() as s:
            repo = TaskRepository(s)
            run = repo.start("weights_update", label="monthly")
            try:
                from app.backtest.weights import update_model_weights

                res = update_model_weights(s)
                repo.finish(run, "success", str(res))
                logger.info(f"[scheduler] weight update done: {res}")
            except Exception as e:
                repo.finish(run, "failed", str(e))
                logger.exception(f"[scheduler] weight update failed: {e}")
    except Exception as e:
        logger.exception(f"[scheduler] weights monthly job error: {e}")


def _transmission_weekly_job() -> None:
    """§16.2 第 2 层：数据驱动动态权重每周重算（corr/granger/te/var/blend）"""
    logger.info("[scheduler] transmission weekly recalc start")
    try:
        with session_scope() as s:
            repo = TaskRepository(s)
            run = repo.start("transmission_calc", label="weekly")
            try:
                from app.sectors.dynamic_weights import calc_dynamic_weights

                stats = calc_dynamic_weights(s)
                repo.finish(run, "success", str(stats)[:400])
                logger.info(f"[scheduler] transmission recalc done: {stats}")
            except Exception as e:
                repo.finish(run, "failed", str(e))
                logger.exception(f"[scheduler] transmission recalc failed: {e}")
    except Exception as e:
        logger.exception(f"[scheduler] transmission weekly job error: {e}")


def _lstm_weekly_job() -> None:
    """⑱ LSTM 周度重训：全部主连品种，权重落 /app/runtime/lstm"""
    logger.info("[scheduler] lstm weekly retrain start")
    try:
        with session_scope() as s:
            repo = TaskRepository(s)
            run = repo.start("lstm_train", label="weekly")
            try:
                from app.features.pipeline import build_features
                from app.predictors.lstm_train import _ensure_torch, train_symbol

                torch = _ensure_torch()
                settings = get_settings()
                seq_len = settings.yaml.predict.lstm.seq_len
                epochs = settings.yaml.predict.lstm.epochs
                os.environ.setdefault("LSTM_WEIGHT_DIR", settings.yaml.predict.lstm.weight_dir)

                ok, total = 0, 0
                for spec in settings.main_contracts:
                    total += 1
                    try:
                        snap = build_features(s, spec.symbol)
                        # §16.4 多变量：附加传导特征 v1
                        from app.features.transmission import build_transmission_frame

                        tframe = build_transmission_frame(s, spec.symbol, end=snap.last_date)
                        r = train_symbol(
                            torch, spec.symbol, snap.rets,
                            dates=snap.dates, extra=tframe,
                            seq_len=seq_len, epochs=epochs,
                        )
                        ok += 1 if "error" not in r and "skipped" not in r else 0
                    except Exception as e:
                        logger.warning(f"[lstm] {spec.symbol} 训练失败: {e}")
                repo.finish(run, "success", f"{ok}/{total} symbols")
                logger.info(f"[scheduler] lstm weekly retrain done {ok}/{total}")
            except Exception as e:
                repo.finish(run, "failed", str(e))
                logger.exception(f"[scheduler] lstm weekly retrain failed: {e}")
    except Exception as e:
        logger.exception(f"[scheduler] lstm weekly job error: {e}")


def main() -> None:
    setup_logging()
    settings = get_settings()
    logger.info(f"[scheduler] starting role={settings.env.ROLE}")

    sched = _build_scheduler()

    def _shutdown(*_):
        logger.info("[scheduler] shutdown signal received")
        try:
            sched.shutdown(wait=False)
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # 启动时立刻执行一次（方便调试），由 RUN_ON_BOOT=0 关闭
    if os.getenv("RUN_ON_BOOT", "1") == "1":
        time.sleep(3)  # 等 DB ready
        _ingest_job("boot")

    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        _shutdown()


if __name__ == "__main__":
    main()