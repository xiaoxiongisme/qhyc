-- CL-20261110-13：作业心跳「记账链路缺口」口径订正
--
-- 诊断（2026-11-10 主表聚合 + 宿主 crontab + systemd 日志，非事件表推断）：
--   v_scheduled_task_status 里 10 条 never_ok / 3 条 stale。其中绝大多数不是作业停摆，
--   而是「作业在跑，但真实状态来源不是 ops.scheduled_task_run」：
--     · cloud_local_sync 只在 CLOUD_SYNC_ENABLED=1 的 sync-only 实例注册，
--       生产 qhyc-scheduler 根本不注册该作业（app/scheduler.py:1813-1834）；
--     · 9 条 tsdb_job_* 由 TimescaleDB 后台 worker 执行，真实状态在
--       timescaledb_information.job_stats（实测 8 个 job 全部 Success、
--       next_start 在未来），从不写宿主流水；tsdb_job_1003 在 jobs 表已不存在；
--     · sp_build_l1_from_minute 生产走 python 合成（synthesize_bars_incremental），
--       不再 CALL SP，故 record_execution 不写（app/scheduler.py:3255-3283）；
--     · backtest_weekly 的 cron 0 7 * * 5 实证下次触发 2026-11-14 周六 07:00，
--       11-08 由宿主独立进程手工重跑（CL-20261108-02），
--       public.backtest_result 11-08 11:28 有 10,150 行产出；
--       11-01→11-14 本就不该有调度流水，固定 9 天阈值把合法空档误判成停摆。
--
-- 原则：**不伪造补录流水**（skill 坑 #4：事件表不是状态表）。
--   不由流水监控的作业置 expected_gap_days=NULL → 视图显示 no_heartbeat（诚实），
--   并在 meta.status_source 声明真实状态源；新增 OPS-TASK-STATUS-SOURCE 断言
--   从 timescaledb_information.job_stats / public.backtest_result 直接判健康。
--   真会写流水的 cron_compress_chunks / cron_bar_consistency_check **保留心跳**，
--   不用置 NULL 掩盖历史失败（两者根因已由 CL-20261110-14 与网格清理消除）。
--
-- 备份：l3_ref.bak_cl20261110_13_task_dict（77 行）
-- 回滚：UPDATE ops.scheduled_task t SET expected_gap_days=b.expected_gap_days,
--       enabled=b.enabled, meta=b.meta FROM l3_ref.bak_cl20261110_13_task_dict b
--       WHERE t.task_id=b.task_id;

-- 1) sync-only 专用实例作业：生产调度器不注册
UPDATE ops.scheduled_task
SET expected_gap_days = NULL,
    meta = meta || '{"status_source": "CLOUD_SYNC_ENABLED=1 sync-only 实例注册；生产 qhyc-scheduler 不注册，无 ops.scheduled_task_run 流水"}'::jsonb,
    updated_at = now()
WHERE task_id = 'cloud_local_sync';

-- 2) TimescaleDB 后台作业：真实状态源是 job_stats
UPDATE ops.scheduled_task
SET expected_gap_days = NULL,
    meta = meta || '{"status_source": "timescaledb_information.job_stats（TSDB 后台 worker，从不写 ops.scheduled_task_run）"}'::jsonb,
    updated_at = now()
WHERE executor = 'timescaledb_job' AND enabled;

-- 3) jobs 表里已不存在的陈旧登记（job_id=1003）
UPDATE ops.scheduled_task
SET enabled = false,
    meta = meta || '{"status_source": "已退役：timescaledb_information.jobs 无 job_id=1003"}'::jsonb,
    updated_at = now()
WHERE task_id = 'tsdb_job_1003';

-- 4) L1 增量聚合已切 python 合成，SP 分支不再调用
UPDATE ops.scheduled_task
SET expected_gap_days = NULL,
    meta = meta || '{"status_source": "app.ingest.synthesizer.synthesize_bars_incremental（python 模式）；不再 CALL SP，故不写 record_execution"}'::jsonb,
    updated_at = now()
WHERE task_id = 'sp_build_l1_from_minute';

-- 5) 周度回测：保留心跳，阈值由 9 调 14（周频 7 天 + 一次漏跑容错）。
--    实际告警判据已是 cron-aware（OPS-JOBHB-CRON-AWARE），此处只让展示视图一致。
UPDATE ops.scheduled_task
SET expected_gap_days = 14,
    meta = meta || '{"status_source": "public.backtest_result.created_at（11-08 宿主独立进程手工重跑 CL-20261108-02）；cron 0 7 * * 5 下次触发 2026-11-14 周六，中间本不应有调度流水"}'::jsonb,
    updated_at = now()
WHERE task_id = 'backtest_weekly' AND expected_gap_days = 9;
