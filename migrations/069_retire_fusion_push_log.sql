-- 069_retire_fusion_push_log.sql
-- CL-20261109-18：退役 fusion_push_log（推送原文留痕表）
--
-- 为什么删而不是留
-- ----------------
-- 该表存的是**推送正文**（标题/正文/是否送达），唯一消费方是 `GET /fusion/push_log`
-- 这个「怕消息被微信折叠，回头翻历史」的查询接口。它不参与任何计算、不喂任何策略。
--
-- 生产实测（删前）：
--   · 行数 325，最后写入 2026-11-06 23:04，最后一次 delivered=t 在 11-06 10:03；
--   · 2026-11-08 起生产推送源唯一化为 V5X.3（cfg_feature_switch.fusion_v34_live_enabled=f），
--     fusion_scan 作业在开关判断后**直接 return**，写本表的 _deliver/_push_startup_notice
--     已成不可达代码；
--   · 它停止增长以后，ops_monitor 的 push_silence/push_unsent 仍在查它 ⇒ 变成
--     「已 79 小时无成功推送」的**永久误报**，每 10 分钟一条（CL-20261109-15 已修）。
--
-- 也就是说：留着它没有收益，却一再把监控带偏（「数据源过期」这类 bug 只会在
-- 表还在的时候发作，表没了就彻底断根）。
--
-- 归档
-- ----
-- 325 行已导出：/home/ubuntu/qhyc_work/backup/fusion_push_log_20261109.csv
-- 回滚：本迁移不可自动回滚。若确需恢复，用上表 CSV +
--       app/strategies/fusion_signal.py 里的 FusionPushLog 定义（已删，见 git 历史
--       本迁移之前的最后一个 commit）重建——但没有任何代码会再往里写，
--       恢复的意义仅是考古。
--
-- 依赖检查（删前已确认）：无视图依赖（pg_depend 0 行）、无函数体引用
-- （pg_proc prosrc LIKE '%fusion_push_log%' 0 行）；`app_state.fusion_push_log`
-- 从未创建过（数据层分层脚本里登记过映射，但该表一直留在 public）。
--
-- 语义替代：要查「现在发了哪些信号 / 有没有送达」，用 v5x_signal_log
--   （结构化字段 + notify_ok + 失败原因，比推送正文可算得多）。

BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'app_state' AND c.relname = 'fusion_push_log') THEN
        RAISE NOTICE 'app_state.fusion_push_log 存在，一并删除（分层迁移未实际执行的残留）';
        DROP TABLE IF EXISTS app_state.fusion_push_log;
    END IF;
END $$;

-- 序列 fusion_push_log_id_seq 与主键索引随表一并删除，无需单独 DROP。
DROP TABLE IF EXISTS public.fusion_push_log;

COMMIT;
