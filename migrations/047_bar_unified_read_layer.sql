-- 047_bar_unified_read_layer.sql
-- 为多源 bar 表提供「统一读取层」视图，并给出 src 覆盖，杜绝「只看单源误判覆盖」。
--
-- 【背景：附带的调查结论 —— 不需要物理合并】
--   提问："数据源重复，是同一张表还是多张表？多张表就合并成一张表。"
--   实测结论：**本来就是同一张表，且物理上不可能重复**。证据三条：
--
--   1. l0_raw.hourly_bar / daily_bar 的**主键都不含 src**：
--        hourly_bar_pkey  UNIQUE (symbol, trade_datetime)
--        daily_bar_pkey   UNIQUE (symbol, trade_date)
--      → 同一 (symbol, 时刻) 全局只能有一行，第二个源想写同一根会被主键拒绝。
--        所谓"多源"其实是**同一张表内的时间接力**，不是两张表。
--
--   2. 各 src 行数之和恰好等于总行数：
--        hourly_bar: agg_1m 732,115 + akshare 409,343 = 1,141,458 = 总行数 ✓
--        daily_bar : akshare 158,781 + csv 9,647 + backfill_tq 1,092
--                    + backfill 477 = 169,997 = 总行数 ✓
--      → 没有任何一行被冲突丢弃，也没有重复行。
--
--   3. 实测多篇组合数为 0：
--        hourly_bar 的 agg_1m ∩ akshare = 0
--        daily_bar  的 4 源两两重叠  = 0，完全重复键 = 0
--
--   另注：public.hourly_bar / public.daily_bar 只是指向 l0_raw 的 VIEW
--        （见 information_schema.tables 的 table_type 与 pg_views.definition），
--        也不是"第二张表"。
--
-- 【真正的问题不是重复，是 src 让下游困惑】
--   2026-10-30 的一次误判根因：回测参数 FusionBacktestParams.src 取自
--   fusion 配置 = 'akshare'，于是 `WHERE src='akshare'` 只读到 2020 年之后，
--   得出"小时线只有 4 年"的错误结论。实际 agg_1m 覆盖到 2026-06-12 之前。
--   → 根因是**下游必须先知道该用哪个 src**，而不是数据本身有问题。
--
-- 【修法】
--   不改任何数据、不动主键，只加一层**只读视图**：
--     l0_raw.hourly_bar_uni   —— 不限 src 的全时段小时线（列同原表，另加 src 溯源）
--     l0_raw.daily_bar_uni    —— 不限 src 的全时段日线
--     l0_raw.bar_src_coverage —— 按 (symbol, src) 的覆盖体检表，用于快速定位
--                                "某品种某时段到底由哪个 src 提供"
--   三者均为普通 VIEW：不占空间、始终最新、DROP 即可回滚。
--
-- 【幂等】
--   CREATE OR REPLACE VIEW 天然幂等，可重复执行。

BEGIN;

-- ── 1. 小时线统一读取层 ────────────────────────────────────────────────
CREATE OR REPLACE VIEW l0_raw.hourly_bar_uni AS
SELECT
    symbol,
    trade_datetime,
    open,
    high,
    low,
    close,
    volume,
    oi,
    ret,
    src,            -- 保留溯源：这根 bar 由哪个采集源提供
    created_at
FROM l0_raw.hourly_bar;

COMMENT ON VIEW l0_raw.hourly_bar_uni IS
    '统一读取层：不限 src 的全时段小时线。一个 (symbol, trade_datetime) 恒一行'
    ' —— 主键 UNIQUE(symbol, trade_datetime) 已保证，无需去重。'
    ' 用途：回测/分析若不关心采集源，应读本视图而非带 WHERE src=... 的基础表，'
    ' 否则会像 2026-10-30 那样只读到单源、误判数据覆盖范围。';

-- ── 2. 日线统一读取层 ──────────────────────────────────────────────────
CREATE OR REPLACE VIEW l0_raw.daily_bar_uni AS
SELECT
    symbol,
    trade_date,
    open,
    high,
    low,
    close,
    settle,
    volume,
    amount,
    oi,
    ret_close,
    ret_settle,
    ret5,
    ret20,
    src,            -- 保留溯源
    created_at,
    updated_at
FROM l0_raw.daily_bar;

COMMENT ON VIEW l0_raw.daily_bar_uni IS
    '统一读取层：不限 src 的全时段日线。同 hourly_bar_uni，'
    '主键 UNIQUE(symbol, trade_date) 已保证每个交易日恒一行。';

-- ── 3. src 覆盖体检视图 ────────────────────────────────────────────────
--    给表下"覆盖到哪年"的结论前必须先看这张 —— 这是 2026-10-30 误判的直接教训。
CREATE OR REPLACE VIEW l0_raw.bar_src_coverage AS
SELECT 'hourly'::text AS tf,
       symbol, src,
       count(*)                                        AS n_bars,
       min(trade_datetime)::date                       AS first_day,
       max(trade_datetime)::date                       AS last_day,
       (current_date - max(trade_datetime)::date)      AS stale_days
FROM l0_raw.hourly_bar
GROUP BY symbol, src
UNION ALL
SELECT 'daily'::text AS tf,
       symbol, src,
       count(*)                                        AS n_bars,
       min(trade_date)                                 AS first_day,
       max(trade_date)                                 AS last_day,
       (current_date - max(trade_date))                AS stale_days
FROM l0_raw.daily_bar
GROUP BY symbol, src;

COMMENT ON VIEW l0_raw.bar_src_coverage IS
    '每个 (周期, 品种, 采集源) 的行数与时间跨度。'
    '用法：SELECT * FROM l0_raw.bar_src_coverage WHERE symbol=''CU888'' ORDER BY first_day;'
    ' 用于确认某品种某时段由哪个 src 提供，以及是否有源停摆(stale_days 异常偏大)。';

COMMIT;
