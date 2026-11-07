-- 迁移 051：task_run 状态枚举新增 'aborted'（2026-11-07）
--
-- 背景
--   `app/scheduler.py::_reap_stale_running`（2026-11-07 新增启动自愈）需要在进程
--   重启后，把上一进程遗留的 `status='running' / finished_at=NULL` 僵尸记录收尾。
--   实测（11-07 21:50）：33/9065 条僵尸，分布 scheduler_resync 28 / fusion_scan 3 /
--   hourly_collect 2，每次重启恰好新增一条（11-06 22:30 resync ↔ 22:31 重启、
--   11-07 14:00 resync ↔ 14:04 重启）——进程被 kill 时作业来不及走 finish()。
--
--   僵尸行污染**监控语义**：任何「查 running 判断是否卡死」的诊断都会被误导
--   （2026-11-07 审计即据此误判"有任务 hang 住"）。
--
--   现有枚举（'running','success','failed','missed','disabled_skipped','timeout'）
--   没有一个能准确表达「进程重启打断、任务未完成即终止」：
--     · failed  —— 任务本身失败（会污染失败率统计与告警判据）
--     · timeout —— 超时（语义不同，且可能触发超时处置逻辑）
--   故新增 'aborted'，仅由启动自愈写入。
--
-- 幂等：DROP CONSTRAINT IF EXISTS + 重建，可重复执行。
-- 影响面：仅放宽枚举约束；既有 9065 行状态值全部落在新枚举内，无数据变更。

DO $$
BEGIN
    ALTER TABLE ops.scheduled_task_run
        DROP CONSTRAINT IF EXISTS scheduled_task_run_status_check;

    ALTER TABLE ops.scheduled_task_run
        ADD CONSTRAINT scheduled_task_run_status_check
        CHECK (status = ANY (ARRAY[
            'running'::text,
            'success'::text,
            'failed'::text,
            'missed'::text,
            'disabled_skipped'::text,
            'timeout'::text,
            'aborted'::text          -- 2026-11-07 新增：进程重启打断的遗留 running
        ]));
END $$;

COMMENT ON COLUMN ops.scheduled_task_run.status IS
    'running/success/failed/missed/disabled_skipped/timeout/aborted；'
    'aborted 仅由 scheduler._reap_stale_running 在启动时写入（上一进程被重启打断的遗留 running）。';
