-- =============================================================================
-- 031_calendar_seed_meta.sql · G4 播种元数据（让"缺失行"不再二义）
-- 日期：2026-10-05
--
-- 问题
--   `futures_rule` 只记录「有数据的交易日」（非交易日 akshare 接口无表 → 种子脚本跳过），
--   因此「某日无行」是**二义**的：既可能是节假日/周末，也可能是**未来尚未播种**。
--   仅凭表内容判断 2026-10-01~10-07（国庆休市）会落进"未知"分支；若退化为「周一~周五」，
--   就会把 10-01（周四·休市）**误判为交易日**——静默错，且不报错。
--
-- 处置
--   显式记录「已播种截止日」seeded_through（= 种子脚本 --end 跑完的日期）。
--   app/data/trade_calendar.py 据此三分：
--     ① 有行                      → 交易日（权威）
--     ② 无行 且 <= seeded_through  → **确定的非交易日**（权威：休市/周末）
--     ③ 无行 且 >  seeded_through  → 真正未知（strict 抛错 / 否则告警退化）
--
-- 幂等：IF NOT EXISTS；回滚：DROP TABLE。
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS cfg_calendar_seed_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE cfg_calendar_seed_meta IS
    'G4 期货日历播种元数据。seeded_through = seed_futures_rule.py 已跑到的日期（YYYY-MM-DD）。'
    '用于区分「已确认的非交易日」与「未来未播种」——因 futures_rule 只记交易日，缺失行本二义。';

COMMIT;

-- 验收：SELECT key, value FROM cfg_calendar_seed_meta;   -- 播种脚本运行后应有 seeded_through
-- 回滚：DROP TABLE IF EXISTS cfg_calendar_seed_meta;
