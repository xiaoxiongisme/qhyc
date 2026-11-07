-- 迁移 050：因子级新鲜度巡检作业 + 作业字典 schedule 补真实表达式（2026-11-07）
--
-- 背景
--   ① `app/ops/alerting.py::_check_freshness` 是**表级**检查：只看 factor_value 整表的
--      max(trade_date) ⇒ **发现不了「某个因子停更」**。实测事故：22 个已注册因子里
--      16 个停在 2026-09-30，而整表 max 是 11-06 ⇒ 表级巡检一直显示「正常」，
--      **断更 40 天无人发现**（本项目「开关看似生效、实则静默空转」的又一例）。
--   ② `ops.scheduled_task` 里 `collect_roll_yield` 的 schedule = 'unknown'，
--      而 `app/scheduler.py` 实际已注册该作业（19:10）⇒ **字典与代码不一致**
--      ⇒ 巡检/自检无法校验它是否在跑（同型：代码内字典与库表不一致无任何报警）。
--
-- 本迁移做两件事（**不改任何数据口径**，只补「谁来生产、多久跑一次」的控制面）：
--   1. 新增作业字典行 factor_freshness_daily（19:30，依赖链末端）。
--   2. 把 collect_roll_yield 的 schedule 从 'unknown' 补成真实 cron '10 19 * * *'。
--
-- 幂等：全部 ON CONFLICT DO UPDATE / DO NOTHING，可重复执行。

-- 1) 新增「因子级新鲜度巡检 + 自动补算」作业
--    ⚠ 列清单照 049（ops.scheduled_task.name 是 NOT NULL；executor/location 等有默认值）
INSERT INTO ops.scheduled_task
  (task_id, name, purpose, executor, schedule, schedule_desc, target, location, enabled, expected_gap_days)
VALUES
  ('factor_freshness_daily',
   '因子级新鲜度巡检+自动补算',
   '每日 19:30 逐个 factor_registry.factor_id 查 factor_value 的 max(trade_date)，'
   '滞后>3 天即断供；断供按组自动补算（幂等 UPSERT），补后仍断供则硬告警。'
   '补这一环是因为表级巡检（alerting._check_freshness）只看整表 max，'
   '曾出现 16 个因子停在 2026-09-30 而整表 max 是 11-06、断更 40 天无人发现',
   'apscheduler', '30 19 * * *',
   '每日 19:30（依赖链末端：16:20 v1v6 / 16:35 A组 / 17:45 会员汇总 / 18:00 B+C / 19:10 roll_yield 之后）',
   'app/scheduler.py::_factor_freshness_job', 'cloud-container', TRUE, 3)
ON CONFLICT (task_id) DO UPDATE
    SET schedule = EXCLUDED.schedule,
        schedule_desc = EXCLUDED.schedule_desc,
        purpose = EXCLUDED.purpose,
        expected_gap_days = EXCLUDED.expected_gap_days;

-- 2) 补 collect_roll_yield 的真实表达式（代码里是 19:10，见 scheduler.py::_collect_roll_yield_job）
UPDATE ops.scheduled_task
   SET schedule = '10 19 * * *'
 WHERE task_id = 'collect_roll_yield'
   AND (schedule IS NULL OR schedule IN ('unknown', '', '按需'));

-- 3) 自检：确认两条都落地（迁移脚本本身不校验，由调用方查库验收）
--    SELECT task_id, schedule, enabled FROM ops.scheduled_task
--     WHERE task_id IN ('factor_freshness_daily', 'collect_roll_yield');
