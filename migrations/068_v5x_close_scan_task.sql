-- =====================================================================
-- 068_v5x_close_scan_task.sql
-- V5X 收盘即时扫描链：字典登记（CL-20261109-17，2026-11-09）
-- ---------------------------------------------------------------------
-- 一、为什么要有这个作业
--
--   用户要求「每小时 K 线结束后 +10s 扫描并推送信号」。原链路做不到，实测：
--     · `v5x_scan` 固定 :05/:35，与 60m 真实收盘标签（10:00/11:15/14:15/15:00、
--       夜盘 22:00/23:00/00:00/01:00/02:00/02:30）无对应关系 —— :35 扫到 14:15
--       那根已过 20 分钟，:05 扫时那根还没收盘；
--     · 分钟采集 + L1 合成只在 :07/:37 跑（实测 14:07:00→14:10:05）；
--       BA(`l2_adj.bar_*_ba`) 一天只重建 3 次（02:55/11:55/15:55），
--       实测 11-09 的 15:00 那根要到 15:55 才进 BA ⇒ 收盘后一小时内扫描读旧表；
--     · `synthesize_bars_incremental` 会写「在途半截桶」（实测 14:10 已写出
--       标签 14:15 的半截 bar），而 `scan_once` 只按 `bucket <= now` 过滤 ⇒
--       可能拿半根 K 线出信号，且 `dedupe_key` 会把这个错误结果永久固化。
--
--   新作业 `v5x_close_scan`：每根 60m 收盘 +10s 跑一条完整链
--   （补 L0 → 合成 L1 → 刷 BA(60m) → 扫描推送），见 `app/ingest/close_pipeline.py`。
--   与 `v5x_scan`（:05/:35）并存：前者准时，后者兜底不漏。
--
-- 二、处置（为什么 executor='manual'）
--
--   `_reregister_schedules_from_db` 只读 `executor='apscheduler'` 的行，并对
--   **合法 5 段 cron** 调 `CronTrigger.from_crontab` 覆盖代码触发器。本作业的
--   触发时刻是「收盘 + 10 秒」：
--     · 分钟粒度的 5 段 cron **表达不了 second=10**；
--     · 60m 标签是 10:00/11:15/14:15/15:00 这种非整点集合，单个 cron 也写不出来
--       （代码里用 OrTrigger 组合 6 个 CronTrigger 表达）。
--   所以这里刻意登记为 `executor='manual'` + `schedule='manual'`：字典只记录
--   **意图**，永不覆盖代码触发器。真实节奏写在 `meta.code_cron` 里，由守卫
--   `V5X-CLOSE-SCAN-WIRED` 反向校验（从 `live.CLOSE_HM` / `live.SCAN_SECOND`
--   推出期望值，不从别处抄常量）。
--
--   ★ 反例警告（CL-20261108-28 / 062 的教训）：任何人「顺手把 manual 补成
--     `5,35 * * * *`」都会让 resync 把收盘链退化成整分触发，且**静默**。
--
-- 幂等：INSERT ... ON CONFLICT DO NOTHING
-- 回滚：DELETE FROM ops.scheduled_task WHERE task_id='v5x_close_scan';
-- =====================================================================

BEGIN;

INSERT INTO ops.scheduled_task
    (task_id, name, purpose, executor, schedule, schedule_desc, target, location,
     enabled, owner, tags, meta)
VALUES
    ('v5x_close_scan',
     'V5X 收盘即时扫描（60m 收盘 +10s）',
     '每根 60m 收盘后 10 秒跑完整数据链（补 L0 → 合成 L1 → 刷 BA → 扫描）并推送信号；'
     || '失败时信号仍在 v5x_signal_log，2h 内下一轮补发',
     'manual',
     'manual',
     '60m 收盘 +10s：日盘 10:00/11:15/14:15/15:00，夜盘 22:00/23:00/00:00/01:00/02:00/02:30'
     || '（代码 OrTrigger 注册，字典仅登记意图，勿改成 5 段 cron）',
     'app.scheduler:_v5x_close_scan_job',
     'cloud',
     true,
     'qhyc',
     ARRAY['v5x', 'strategy', 'realtime'],
     ('{"code_cron": "OrTrigger(CronTrigger(hour=H, minute=M, second=10) for (H,M) in live.CLOSE_HM)", '
     || '"source_of_truth": "app/strategies/v5x/live.py::CLOSE_HM + SCAN_SECOND", '
     || '"do_not_convert_to_crontab": true}')::jsonb
    )
ON CONFLICT (task_id) DO NOTHING;

COMMIT;
