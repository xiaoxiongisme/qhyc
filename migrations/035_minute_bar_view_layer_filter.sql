-- 035: public.minute_bar 兼容视图恢复「仅 888 主连」语义
-- 日期: 2026-11-06
--
-- 背景
-- ----
-- 全合约采集（scripts/fetch_contract_1min.py，cron_fetch_1min）上线后，
-- l0_raw.minute_bar 同时存在三层数据：
--     layer='continuous'  888 主连，82 品种
--     layer='contract'    各品种全部上市合约，668 个
--     layer='index'       8888 指数
-- public.minute_bar 是历史遗留的 15 列兼容视图，原定义无条件
-- `SELECT ... FROM l0_raw.minute_bar`，因此会把 668 个合约一并暴露给所有
-- 裸引用下游，破坏既有口径假设：
--   * app/data/caliber.py ("1m","continuous") 路由 —— 回测/对账基准应只取主连
--   * app/ingest/data_selfcheck.py 的 max(ts) 停更检测 —— 会被更晚的合约数据掩盖
--   * app/execution/validator.py 注释明确「库内全为 888 连续、无真实合约行」
--   * scripts/ 下若干一次性导入/巡检脚本的 SELECT DISTINCT symbol FROM minute_bar
--
-- 处置
-- ----
-- 兼容视图加 `layer = 'continuous'` 过滤，列名/顺序/类型全部不变（15 列），
-- 因此：
--   * 无需改任何下游代码，也无需重启 api / scheduler / pipeline，DDL 即时生效；
--   * 需要访问合约层的代码（contract_minute_collector、synthesizer）都显式查
--     l0_raw.minute_bar，不受影响；
--   * 简单视图带 WHERE 仍可更新，minute_collector 的 upsert（默认
--     layer='continuous'）行为不变。
--
-- 前置核查：public.minute_bar 无反向依赖对象（pg_depend 查询 0 行），可安全替换。

BEGIN;

CREATE OR REPLACE VIEW public.minute_bar AS
SELECT symbol,
       ts,
       open,
       high,
       low,
       close,
       volume,
       amount,
       open_interest,
       high_limit,
       low_limit,
       pre_close,
       settle_price,
       contract,
       src
FROM l0_raw.minute_bar
WHERE layer = 'continuous';

COMMIT;

-- 验收（期望 non_main = 0、syms 与 l0_raw continuous 层一致）
-- SET max_parallel_workers_per_gather = 0;
-- SELECT count(DISTINCT symbol) AS syms,
--        count(DISTINCT symbol) FILTER (WHERE symbol ~ '^[A-Za-z]+888$') AS main888,
--        count(DISTINCT symbol) FILTER (WHERE symbol !~ '^[A-Za-z]+888$') AS non_main
-- FROM public.minute_bar WHERE ts >= now() - interval '3 days';
--
-- 回滚（如需恢复「暴露全部层」）：
-- CREATE OR REPLACE VIEW public.minute_bar AS
-- SELECT symbol, ts, open, high, low, close, volume, amount, open_interest,
--        high_limit, low_limit, pre_close, settle_price, contract, src
-- FROM l0_raw.minute_bar;
