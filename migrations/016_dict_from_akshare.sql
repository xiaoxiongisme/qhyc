-- =============================================================================
-- 016 · 字典层：保证金入字典 + 品种官方名/规格以 akshare 为权威
-- 日期：2026-10-03　用户裁定：4 处手续费冲突按 akshare 口径；LR 晚籼稻不纳入；
--                     百分比(‰)手续费以交易所通知为权威，akshare 负责规格类
-- 适用：本地库与云端库同构执行（幂等，可重复运行）
--
-- 真源分工（2026-10-03 实测：akshare 87 品种 × 交易所通知 81 品种交叉比对后确定）
--   合约乘数 / 最小跳动        -> akshare   （品种内 100% 内部一致；vs 参数表一致）
--   官方中文名 / 交易所        -> akshare   （补齐此前 16 个无名品种）
--   保证金率 / 保证金每手      -> akshare   （合约级有差异，故同时存区间）
--   固定值手续费(元/手)        -> akshare   （vs 通知 4 处冲突，已裁定以 akshare 为准）
--   百分比手续费(‰)           -> 交易所通知 （★akshare 此处不可用，见下）
--
-- ⚠ 为何百分比费率不用 akshare（实测结论，勿轻易改回）
--   ak.futures_fees_info() 对 40 个百分比品种的 `开仓费用/手` 恒为 0.01（≈免费），
--   `开仓费率` 为 0.000001/0.000101/0.000051 等值，与交易所通知反推对比偏离 2~20 倍
--   且无固定比例（RB 0.101‰ vs 0.2‰、CU 0.051‰ vs 0.5‰、SA 0.101‰ vs 2‰）。
--   若误用 akshare 覆盖百分比费率，约 49% 品种手续费将变成近零 → 回测成本严重低估。
-- =============================================================================

BEGIN;

ALTER TABLE dim_variety ADD COLUMN IF NOT EXISTS margin_rate_long     NUMERIC(10,6);
ALTER TABLE dim_variety ADD COLUMN IF NOT EXISTS margin_rate_short    NUMERIC(10,6);
ALTER TABLE dim_variety ADD COLUMN IF NOT EXISTS margin_per_lot_long NUMERIC(16,4);
ALTER TABLE dim_variety ADD COLUMN IF NOT EXISTS margin_min_rate     NUMERIC(10,6);
ALTER TABLE dim_variety ADD COLUMN IF NOT EXISTS margin_max_rate     NUMERIC(10,6);
ALTER TABLE dim_variety ADD COLUMN IF NOT EXISTS exchange_name_src    TEXT;
ALTER TABLE dim_variety ADD COLUMN IF NOT EXISTS official_name        TEXT;

COMMENT ON COLUMN dim_variety.margin_rate_long IS
    '做多保证金率（小数，0.1=10%）。品种级基线，来自 akshare futures_fees_info。'
    '⚠ 交易所常按合约/阶段给不同保证金，故同时记录 margin_min_rate/margin_max_rate 区间。';
COMMENT ON COLUMN dim_variety.official_name IS
    '交易所官方品种全称，来自 akshare（如 A=黄大豆1号、MA=甲醇N、L=线型低密度聚乙烯）。'
    '与 variety_name（行情源简称，如 A=豆一、L=塑料）并存：系统既有查询用简称，'
    '核对交易所口径用全称，故**不互相覆盖**。';
COMMENT ON COLUMN dim_variety.exchange_name_src IS
    '中文名来源：akshare=交易所官方全称（如"精对苯二甲酸"）；'
    'futures_symbol=行情源简称（如"PTA"）；alias_of:*=别名镜像行。';

-- 修正别名错误：胶版纸(OP) 曾被误映射到 SP。
-- 实测 akshare：SHFE op=「胶版纸」乘数 40/跳动 2；SHFE sp=「漂针浆」乘数 10/跳动 2。
-- 二者是不同产品（涂布纸 vs 木浆），此前把「胶板印刷纸」映到 SP 属错误映射。
UPDATE dim_variety
   SET is_active = false,
       source = COALESCE(source,'') || '+deprecated_wrong_sp_alias',
       updated_at = now()
 WHERE variety_code = 'SP'
   AND source LIKE '%deprecated_wrong_sp_alias%';

COMMIT;
