# -*- coding: utf-8 -*-
"""周期性作业注册清单（心跳巡检的**单一事实源**）。

背景（2026-10-13 整改）：
    作业心跳巡检 `_check_job_heartbeat` 原先**从 `task_run` 历史反推**"哪些作业该在跑 +
    该多久跑一次"——靠 `task_type` / `label` 文本里是否含 'month' / 'week' / 'weights' 等
    关键字猜频率，并靠一堆手工维护的例外集（`retired_task_types` / `retired_task_labels` /
    `oneoff_label_markers`）去堵误报。这套机制反复因为「非标准命名分类不全」产生误报：
      · weights_update（label='monthly' 但 task_type 不含 month）→ 被 'weights' 误归 12 天阈值；
      · inventory（label='weekly' 但实为日频盘后）→ 被误判周频空档必报；
      · 一次性手动回填 label（manual_backfill_20261009）→ 被当成独立作业误报停摆；
      · adjust 退役后历史行 → 被当成停摆。
    根因是「心跳巡检自己猜作业清单」，而调度器 `app/scheduler.py::_build_scheduler` 才是
    **真正**注册周期作业的地方。本模块把那张清单抽出来，让两边读同一份事实。

契约：
    · 调度器注册了什么周期作业、以什么 (task_type, label) 写 `task_run`、期望多久跑一次，
      全部以本模块的 `build_job_registry()` 为准。
    · 新增 / 退役 / 改频作业，**只改本模块 + `app/scheduler.py` 的注册**，心跳巡检自动同步，
      不再需要在 `alerting.py` 里补任何例外集。
    · `task_run_based=False` 的作业（如 fusion_scan / 各管线 / 因子计算等）**不写**
      `task_run`，心跳巡检无从用"上次成功时间"判断，故默认不纳入心跳（其存活由调度器
      事件监听 + 数据新鲜度巡检兜底）。若未来要给它们加心跳，应先在作业里 `repo.start/finish`。

⚠ 同步纪律：本清单必须与 `app/scheduler.py::_build_scheduler` 的注册**逐项一致**。
   改了那边忘了改这边（或反之），心跳要么漏监控、要么盯着一个不存在的 (task_type,label)。
   维护时请对照 `_build_scheduler` 逐条核对。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JobSpec:
    """一个已注册的周期作业。

    task_type / label 必须与作业函数里 `repo.start(task_type, label=...)` 写入 `task_run`
    的标识**逐字一致**，否则心跳查不到"上次成功"会误判。
    """

    job_id: str               # scheduler 里的 add_job id，如 "inventory_weekly"
    task_type: str            # 写入 task_run.task_type 的标识（不写则为 ""）
    label: str                # 写入 task_run.label 的子标签（无则为 ""）
    expected_gap_days: int    # 距上次成功超过此天数即告警
    active: bool              # 当前配置下是否注册（受开关控制）
    task_run_based: bool      # 是否写 task_run（从而可被心跳用"上次成功"监控）

    @property
    def key(self) -> str:
        return f"{self.task_type}:{self.label}" if self.label else self.task_type


# 期望空档（天）：日频 3 / 库存(工作日盘后) 4 / 周频 9 / 月频 40。
# 取值原则：略大于真实调度间隔，留出错峰与周末缓冲，避免健康作业在空档期被误报。
_GAP_DAILY = 3
_GAP_INVENTORY = 4          # 周一~周五盘后，周五→下周一约 3 天，给 1 天缓冲
_GAP_WEEKLY = 9             # 周六触发，周六→周六 7 天，给 2 天缓冲
_GAP_MONTHLY = 40           # 每月 1 日，给 10 天缓冲


def build_job_registry() -> list[JobSpec]:
    """返回当前配置下调度器注册的全部周期作业（心跳巡检的单一事实源）。

    读 `app.core.config.get_settings()`，与 `app/scheduler.py::_build_scheduler` 用的是
    **同一份**配置，因此 cron_specs 的 ingest 子标签等不会与 `task_run` 里实际写入的
    label 漂移。配置加载失败时返回空表（心跳退化为"不报"，与旧行为一致，不抛错）。
    """
    try:
        from app.core.config import get_settings
        settings = get_settings()
    except Exception:  # noqa: BLE001 —— 配置不可用时不要拖垮调度器启动
        return []

    jobs: list[JobSpec] = []

    # ---- 采集类：每个 cron_spec 一个 ingest + 一个 predict（label 相同）----------
    for spec in settings.cron_specs:
        lbl = str(spec.label)
        jobs.append(JobSpec("ingest_" + lbl, "ingest", lbl, _GAP_DAILY, True, True))
        jobs.append(JobSpec("predict_" + lbl, "predict", lbl, _GAP_DAILY, True, True))

    # ---- 周频（周六）：LSTM 重训 / 传导重算 / 回测 --------------------------------
    jobs.append(JobSpec("lstm_weekly_retrain", "lstm_train", "weekly", _GAP_WEEKLY, True, True))
    jobs.append(JobSpec("transmission_weekly_recalc", "transmission_calc", "weekly", _GAP_WEEKLY, True, True))
    jobs.append(JobSpec("backtest_weekly", "backtest", "weekly", _GAP_WEEKLY, True, True))

    # ---- 月频（每月 1 日）：权重更新 --------------------------------------------
    jobs.append(JobSpec("weights_monthly_update", "weights_update", "monthly", _GAP_MONTHLY, True, True))

    # ---- 库存（工作日盘后，受 inventory.enabled 控制）---------------------------
    inv_enabled = bool(getattr(getattr(settings, "inventory_config", None), "enabled", False))
    jobs.append(JobSpec("inventory_weekly", "inventory", "weekly", _GAP_INVENTORY, inv_enabled, True))

    # ---- 基差 / 现货（日频，受 spot_basis.enabled 控制）-------------------------
    sb_enabled = bool(getattr(getattr(settings, "spot_basis_config", None), "enabled", False))
    jobs.append(JobSpec("spot_basis_daily", "spot_basis", "daily", _GAP_DAILY, sb_enabled, True))

    # ---- 以下作业**不写** task_run，心跳巡检无从监控，仅登记备查 ----------------
    # （存活由调度器 EVENT_JOB_ERROR/MISSSED 事件监听 + 数据新鲜度巡检兜底）
    # P0 盘前作战门（迁移 044）：三段交易各一次，与 _build_scheduler 里的 id 保持一致
    preopen_on = bool(__import__("os").getenv("PREOPEN_GATE_ENABLED", "0") == "1")
    fusion_on = bool(getattr(getattr(settings, "fusion", None), "enabled", False))
    hourly_on = bool(getattr(getattr(settings, "hourly_config", None), "enabled", False))
    wr_on = bool(getattr(getattr(settings, "yaml", None), "warehouse_receipt", None) and
                 getattr(settings.yaml.warehouse_receipt, "enabled", False))
    rp_on = bool(getattr(getattr(settings, "yaml", None), "rank_position", None) and
                 getattr(settings.yaml.rank_position, "enabled", False))
    jobs.extend([
        JobSpec("preopen_gate_day_0830", "preopen_gate", "day_0830", _GAP_DAILY, preopen_on, False),
        JobSpec("preopen_gate_day_1255", "preopen_gate", "day_1255", _GAP_DAILY, preopen_on, False),
        JobSpec("preopen_gate_night_2030", "preopen_gate", "night_2030", _GAP_DAILY, preopen_on, False),
        JobSpec("fusion_scan", "fusion_scan", "scan", _GAP_DAILY, fusion_on, False),
        JobSpec("hourly_collect", "hourly_collect", "collect", _GAP_DAILY, hourly_on, False),
        JobSpec("minute_and_bars", "minute_and_bars", "pipeline", _GAP_DAILY, True, False),
        JobSpec("back_adjusted_rebuild", "back_adjusted", "rebuild", _GAP_DAILY, True, False),
        JobSpec("rank_position", "rank_position", "daily", _GAP_DAILY, rp_on, False),
        JobSpec("warehouse_receipt", "warehouse_receipt", "daily", _GAP_DAILY, wr_on, False),
        JobSpec("daily_futures_refresh", "daily_futures_refresh", "refresh", _GAP_DAILY, True, False),
        JobSpec("factor_v1v6_daily", "factor_v1v6", "daily", _GAP_DAILY, True, False),
        JobSpec("factor_ic_monitor_daily", "factor_ic_monitor", "daily", _GAP_DAILY, True, False),
        JobSpec("data_selfcheck_daily", "data_selfcheck", "daily", _GAP_DAILY, False, False),
        JobSpec("ops_backup_daily", "ops_backup", "daily", _GAP_DAILY, True, False),
    ])
    return jobs


def monitored_jobs() -> list[JobSpec]:
    """心跳巡检实际监控的作业（写 task_run 且当前配置激活）。"""
    return [j for j in build_job_registry() if j.task_run_based and j.active]
