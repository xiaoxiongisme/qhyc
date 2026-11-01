-- =============================================================
-- 048_register_bar_consistency_cron.sql
-- 登记「bar 一致性每日巡检」到 ops.scheduled_task（控制面单一事实源）
-- -------------------------------------------------------------
-- 配套脚本：scripts/validate/run_bar_consistency_daily.py
--   —— 串行跑 check_bar_grid_consistency（网格定义两侧一致）
--      与 check_cross_tf_consistency（30m==15m、60m==30m 聚合自洽），
--      任一失败 pushplus 告警。退化会静默重现 2026-10-31 网格/双源污染事故。
--
-- 实际调度在宿主 crontab（task_runner 包装，每日 09:30），本行仅登记 + 留痕，
-- 便于 ops_alert_check 按 expected_gap_days 心跳巡检发现停摆。
--
-- 幂等：可重复执行（ON CONFLICT 只更描述字段，绝不覆盖人控 enabled）。
-- 回滚：DELETE FROM ops.scheduled_task WHERE task_id='cron_bar_consistency_check';
-- =============================================================

BEGIN;

INSERT INTO ops.scheduled_task
    (task_id, name, purpose, executor, schedule, schedule_desc, target, location, enabled, expected_gap_days)
VALUES
 ('cron_bar_consistency_check',
  'bar 一致性每日巡检',
  '每日 09:30 串行跑 check_bar_grid_consistency（增量合成与全量重建两侧网格定义一致）+ check_cross_tf_consistency（30m==15m、60m==30m 两两聚合自洽）；任一失败 pushplus 告警。退化会静默重现 2026-10-31 网格/双源污染事故',
  'crontab', 'unknown', '每日 09:30（宿主 crontab，task_runner 包装）',
  'scripts/validate/run_bar_consistency_daily.py', 'cloud-host', TRUE, 2)
ON CONFLICT (task_id) DO UPDATE SET
    name = EXCLUDED.name,
    purpose = EXCLUDED.purpose,
    executor = EXCLUDED.executor,
    schedule = EXCLUDED.schedule,
    schedule_desc = EXCLUDED.schedule_desc,
    target = EXCLUDED.target,
    expected_gap_days = EXCLUDED.expected_gap_days,
    updated_at = now();

COMMIT;
