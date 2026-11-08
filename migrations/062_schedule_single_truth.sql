-- =====================================================================
-- 062: 调度时间单一真相（字典 vs 代码）  CL-20261109-07
-- ---------------------------------------------------------------------
-- 一、问题
--
--   `_reregister_schedules_from_db()` 在**启动时**用
--   `ops.scheduled_task.schedule`（executor='apscheduler' 的行）覆盖代码里的
--   `CronTrigger`。于是同一件事有两个真相，且**字典赢**。实测（2026-11-09）：
--
--     | task        | 字典值        | 代码值   | 后果 |
--     |-------------|---------------|----------|------|
--     | fusion_scan | 1,31 * * * *  | 5,35     | 回切 V3.4 会在 :01/:31 触发，早于 :00 的 hourly_collect 落盘 |
--     | v5x_scan    | unknown       | 5,35     | **靠解析失败幸存**：executor='manual' 根本不被读取，且 'unknown' 不是 5 段 cron |
--
--   v5x_scan 这行最危险：它看起来"登记过了"，实际是两条巧合（executor 不是
--   apscheduler + 值不是合法 cron）叠加才保住正确节奏。任何人"顺手把 unknown
--   补成合法 cron"，双跑会静默变单跑，11:15 / 14:15 两根非整点日盘 bar 永远
--   不被评估（CL-20261108-28 栽过一次）。
--
-- 二、处置
--
--   · 把两行都改成与代码一致的 `5,35 * * * *`（分钟级，不涉及星期位，
--     不踩 apscheduler `from_crontab` 的 0=周一 坑）；
--   · 新增 `v5x_snapshot` 行，登记其**意图**（日盘收盘后 15:05 推理论持仓清单，
--     CL-20261109-08）。这里刻意**不写 cron**：`schedule='manual'` 不是 5 段
--     cron，`_parse_db_cron` 解析为 None → 永远沿用代码里的
--     `CronTrigger(day_of_week="mon-fri", hour=15, minute=5)`。
--
--     ★ 为什么不写 `5 15 * * 1-5`：`CronTrigger.from_crontab` 的星期位是
--       **0=周一**，与标准 crontab 差一天。写 `1-5` 会被解析成**周二~周六**
--       （CL-20261107-03 的周度作业就是这么静默跑错 3 周的）。
--       与其写一个"看着对、按 apscheduler 语义是错"的字符串，不如让它保持惰性，
--       把真实节奏记在 `meta.code_cron` 里，由 `OPS-SCHEDULE-PARITY` 断言守着。
--
-- 三、配套断言
--
--   `OPS-SCHEDULE-PARITY`（block）：
--     · fusion_scan / v5x_scan 的字典 schedule 必须等于代码节奏
--       （从 `SCAN_MINUTES` 推出来，不从别处抄常量）；
--     · scheduler.py 里 fusion 的 `minute="5,35"` 字面量必须与 SCAN_MINUTES 同源；
--     · v5x_snapshot 必须存在、executor='manual'、schedule 不是合法 5 段 cron、
--       且 `meta.code_cron` 非空（意图不能丢）。
--
-- 幂等：UPDATE 到固定值 + INSERT ... ON CONFLICT DO NOTHING，可重复应用。
-- =====================================================================

BEGIN;

UPDATE ops.scheduled_task
   SET schedule     = '5,35 * * * *',
       schedule_desc = '每个整点后 :05 / :35（小时线落盘后；与 hourly_collect :00 错开）',
       updated_at   = now()
 WHERE task_id IN ('fusion_scan', 'v5x_scan');

INSERT INTO ops.scheduled_task
    (task_id, name, purpose, executor, schedule, schedule_desc, target, location,
     enabled, owner, tags, meta)
VALUES
    ('v5x_snapshot',
     'V5X 理论持仓快照',
     'V5X 只推事件不推持仓；每日收盘后推一份「按规则现在应该持有什么」，供手工交易核对起点',
     'manual',
     'manual',
     '交易日 15:05（日盘收盘后）—— 代码 CronTrigger 注册，字典仅登记意图',
     'app.scheduler:_v5x_snapshot_job',
     'cloud',
     true,
     'qhyc',
     ARRAY['v5x', 'strategy'],
     '{"code_cron": "CronTrigger(day_of_week=mon-fri, hour=15, minute=5)", ' 
     '"note": "schedule 保持惰性（非 5 段 cron）以免 from_crontab 的 0=周一 语义把 1-5 解析成周二~周六；节奏以代码为准（CL-20261109-07）"}'::jsonb)
ON CONFLICT (task_id) DO NOTHING;

COMMIT;
