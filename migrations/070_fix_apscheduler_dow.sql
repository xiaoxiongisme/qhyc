-- 070_fix_apscheduler_dow.sql
-- CL-20261109-21：修正两个 apscheduler 作业的 DB cron 星期位错位一天
--
-- 背景
-- ----
-- `ops.scheduled_task.schedule` 是频率单一真相，`scheduler_resync`（每 30 分钟）
-- 用 `CronTrigger.from_crontab(expr)` 重注册触发器。APScheduler 的星期位是
-- **0=周一 … 6=周日**，与标准 crontab（0=周日 / 6=周六）**错位一天**。
-- 043 按标准 crontab 语义写了：
--   · inventory_weekly  `0 17 * * 1-5`  ← 本意「工作日」，实际是周二~周六，**周一不跑**
--     （schedule_desc 明确写「工作日 17:00」；代码注册用的是
--      `CronTrigger(day_of_week=inv.run_day)`，cloud.yaml 里 run_day="mon-fri"，正确）
--   · pipeline_check    `5 8 * * 1`     ← 本意「周一 08:05」，实际是周二 08:05
--     （schedule_desc 写「周一 08:05」；app/pipeline/config.py 默认 check_day="mon"）
--
-- 实证（2026-11-09）：
--   · `ops.scheduled_task_run` 里 inventory_weekly 只有 11-03~11-07 五条 success，
--     11-02 与 11-09 两个周一均无流水；
--   · `CronTrigger.from_crontab('0 17 * * 1-5')` 从 11-09 23:50 起的下四次触发
--     = 11-10 Tue / 11-11 Wed / 11-12 Thu / 11-13 Fri；
--   · inventory 表因此缺 11-09，触发 fresh_inventory 告警。
--
-- ⚠ executor='crontab' 的作业（如 cron_sync_account_equity `5 17 * * 1-5`、
--   cron_weekly_pgdump `30 4 * * 0`）由**系统 crontab** 执行，用标准语义，
--   本来就正确，**不要**一起改。
-- ⚠ 三个 `*_weekly` 的 `... * * 5` 是「周六」（见 app/scheduler.py 1853 行注释），
--   在 APScheduler 语义下 5 就是周六，正确，也不要改。
--
-- 幂等：WHERE 带旧值，重复执行零行更新，无副作用。043 保留原值作为历史轨迹，
-- 全新部署按 043 → 070 顺序执行后同样落到正确值。

BEGIN;

UPDATE ops.scheduled_task
   SET schedule = '0 17 * * 0-4',
       schedule_desc = '工作日 17:00（apscheduler 星期位 0=周一…4=周五）',
       updated_at = now()
 WHERE task_id = 'inventory_weekly'
   AND executor = 'apscheduler'
   AND schedule = '0 17 * * 1-5';

UPDATE ops.scheduled_task
   SET schedule = '5 8 * * 0',
       schedule_desc = '周一 08:05（apscheduler 星期位 0=周一）',
       updated_at = now()
 WHERE task_id = 'pipeline_check'
   AND executor = 'apscheduler'
   AND schedule = '5 8 * * 1';

COMMIT;
