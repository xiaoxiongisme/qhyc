#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字典 ↔ 代码注册表同步（2026-10-14，迁移 043 配套维护入口）。

用途：把代码侧真实注册的作业/数据库内作业/存储过程与 ``ops.scheduled_task``
字典对账——**缺行补录、漂移报告**，保证字典表不落后于代码。

纪律（与迁移 043 D2 一致）：
  * 只 INSERT 缺失行（最小行，purpose=待补录）与更新 **代码侧拥有**的
    ``expected_gap_days``；
  * **绝不覆盖人控字段**（enabled/owner/tags/meta）与种子写入的
    描述性字段（name/purpose/schedule/target）——后者只报告漂移，由人决定；
  * 试运行 ``--dry-run`` 只报告不动库。

运行位置：容器内（需 app 代码与 DB 连接）：
    docker exec -w /app -e PYTHONPATH=/app qhyc-api python scripts/ops/sync_task_registry.py
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from app.core.db import session_scope
from app.core.logging import logger

# task_type → 用途说明（补录行用；完整描述以迁移 043 种子为准）
_PURPOSE_HINT = {
    "ingest": "日频采集+预测联动",
    "predict": "采集后预测联动",
    "lstm_train": "LSTM 周重训",
    "transmission_calc": "传导权重重算",
    "backtest": "融合策略回测",
    "weights_update": "模型权重月更",
    "inventory": "库存采集",
    "spot_basis": "基差/现货采集",
    "fusion_scan": "融合信号扫描",
    "hourly_collect": "小时线采集",
    "minute_and_bars": "分钟优先管线",
    "back_adjusted": "后复权物化重建",
    "rank_position": "会员持仓排名采集",
    "warehouse_receipt": "仓单日报采集",
    "daily_futures_refresh": "期货日频刷新",
    "factor_v1v6": "因子日计算",
    "factor_ic_monitor": "因子 IC 监控",
    "data_selfcheck": "数据自检",
    "ops_backup": "运维备份",
    "ops_alert_check": "巡检告警",
    "pipeline_daily": "信号管线日链",
    "pipeline_signal": "盘中/夜盘信号推送",
    "pipeline_check": "管线周自检",
}

# 宿主 crontab / systemd 侧任务 id（容器内无法发现，按迁移 043 登记册核对）
_EXPECTED_HOST_TASKS = [
    "cron_ops_monitor", "cron_ops_monitor_daily", "cron_ops_cleanup",
    "cron_fetch_1min", "cron_compress_chunks", "cron_deploy_gate",
    "cron_weekly_pgdump", "qhyc-scheduler.service", "qhyc-pipeline.service",
    "cloud_local_sync",
]


def _upsert_minimal(s, task_id: str, *, name: str, purpose: str, executor: str,
                    schedule: str, gap: int | None, dry: bool) -> str:
    """缺行补录；已存在则返回 'exists'。返回 inserted/exists。"""
    if dry:
        return "would_insert"
    s.execute(
        text(
            "INSERT INTO ops.scheduled_task "
            "(task_id, name, purpose, executor, schedule, expected_gap_days) "
            "VALUES (:id, :name, :purpose, :executor, :schedule, :gap) "
            "ON CONFLICT (task_id) DO NOTHING"
        ),
        {"id": task_id, "name": name, "purpose": purpose, "executor": executor,
         "schedule": schedule, "gap": gap},
    )
    return "inserted"


def _existing(s) -> dict[str, dict]:
    rows = s.execute(
        text("SELECT task_id, executor, schedule, expected_gap_days, enabled, purpose "
             "FROM ops.scheduled_task")
    ).mappings().all()
    return {r["task_id"]: dict(r) for r in rows}


def main() -> int:
    ap = argparse.ArgumentParser(description="ops.scheduled_task 字典同步")
    ap.add_argument("--dry-run", action="store_true", help="只报告不写库")
    args = ap.parse_args()

    inserted: list[str] = []
    updated: list[str] = []
    drift: list[str] = []
    missing_host: list[str] = []

    with session_scope() as s:
        exist = _existing(s)

        # ---- 1. APScheduler 作业（scheduler_jobs 单一事实源）----
        try:
            from app.scheduler_jobs import build_job_registry

            for spec in build_job_registry():
                tid = spec.job_id
                hint = _PURPOSE_HINT.get(spec.task_type, spec.task_type)
                purpose = f"{hint}（label={spec.label or '-'}；task_run_based={spec.task_run_based}）"
                if tid not in exist:
                    inserted.append(tid)
                    _upsert_minimal(s, tid, name=tid, purpose=purpose + "；待补录",
                                    executor="apscheduler",
                                    schedule="unknown（见 app/scheduler.py 注册处）",
                                    gap=spec.expected_gap_days, dry=args.dry_run)
                elif exist[tid]["expected_gap_days"] != spec.expected_gap_days:
                    updated.append(f"{tid}: gap {exist[tid]['expected_gap_days']}→{spec.expected_gap_days}")
                    if not args.dry_run:
                        s.execute(
                            text("UPDATE ops.scheduled_task SET expected_gap_days = :gap, "
                                 "updated_at = now() WHERE task_id = :id"),
                            {"gap": spec.expected_gap_days, "id": tid},
                        )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[sync_registry] scheduler_jobs 读取失败: {exc}")

        # ---- 2. pipeline worker 作业 ----
        try:
            from app.pipeline import config as pipe_config

            cfg = pipe_config.load()
            pipe_jobs = [("pipeline_daily", "信号管线日链", 3)]
            for slot in cfg.signal_slots:
                hh, mm = str(slot).split(":")[:2]
                pipe_jobs.append((f"pipeline_signal_{hh}{mm}", f"信号推送 {slot}", 3))
            pipe_jobs.append(("pipeline_check", "管线周自检", 9))
            for tid, name, gap in pipe_jobs:
                if tid not in exist:
                    inserted.append(tid)
                    _upsert_minimal(s, tid, name=name, purpose=name + "；待补录",
                                    executor="apscheduler", schedule="unknown（见 worker.py）",
                                    gap=gap, dry=args.dry_run)
                elif exist[tid]["expected_gap_days"] != gap:
                    updated.append(f"{tid}: gap {exist[tid]['expected_gap_days']}→{gap}")
                    if not args.dry_run:
                        s.execute(
                            text("UPDATE ops.scheduled_task SET expected_gap_days = :gap, "
                                 "updated_at = now() WHERE task_id = :id"),
                            {"gap": gap, "id": tid},
                        )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[sync_registry] pipeline config 读取失败: {exc}")

        # ---- 3. TimescaleDB 后台作业（库内实时发现）----
        try:
            ts_jobs = s.execute(
                text("SELECT job_id, application_name, schedule_interval::text AS iv, "
                     "scheduled FROM timescaledb_information.jobs")
            ).mappings().all()
            for j in ts_jobs:
                tid = f"tsdb_job_{j['job_id']}"
                if tid not in exist:
                    inserted.append(tid)
                    _upsert_minimal(s, tid, name=str(j["application_name"]),
                                    purpose="TimescaleDB 内置/策略作业；待补录",
                                    executor="timescaledb_job",
                                    schedule=str(j["iv"]), gap=None, dry=args.dry_run)
                elif exist[tid]["executor"] != "timescaledb_job":
                    drift.append(f"{tid}: executor {exist[tid]['executor']}≠timescaledb_job")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[sync_registry] timescaledb jobs 读取失败: {exc}")

        # ---- 4. 存储过程（业务 schema 白名单发现）----
        try:
            procs = s.execute(
                text(
                    "SELECT n.nspname || '.' || p.proname AS sig "
                    "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE (n.nspname IN ('public','l2_adj') AND p.proname LIKE 'sp_%') "
                    "OR (n.nspname = 'public' AND "
                    "p.proname IN ('dim_symbol_parse','dim_symbol_collect','l1_table_of'))"
                )
            ).scalars().all()
            for sig in procs:
                tid = sig.split(".")[-1]
                if tid not in exist:
                    inserted.append(tid)
                    _upsert_minimal(s, tid, name=tid, purpose="存储过程；待补录",
                                    executor="stored_proc", schedule="按需", gap=None,
                                    dry=args.dry_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[sync_registry] 存储过程发现失败: {exc}")

        # ---- 5. 宿主侧任务在册核对 ----
        for tid in _EXPECTED_HOST_TASKS:
            if tid not in exist:
                missing_host.append(tid)

        # ---- 6. 待补录清单（自动补的行 + 人未补全的行）----
        todo = s.execute(
            text("SELECT task_id FROM ops.scheduled_task "
                 "WHERE purpose LIKE '%待补录%' ORDER BY task_id")
        ).scalars().all()

    print("=" * 60)
    print(f"[sync_registry] 新补录 {len(inserted)} 行: {inserted}")
    print(f"[sync_registry] gap 更新 {len(updated)} 行: {updated}")
    print(f"[sync_registry] 漂移(需人工) {len(drift)} 项: {drift}")
    print(f"[sync_registry] 宿主任务缺失 {len(missing_host)} 项: {missing_host}")
    print(f"[sync_registry] 待补录 {len(todo)} 项: {list(todo)}")
    print("=" * 60)
    if missing_host:
        print("[sync_registry] WARNING 宿主侧任务未登记——请核对迁移 043 种子/crontab",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
