-- 028_main_contract_caliber.sql
-- 任务①（主力合约口径裁决 丙/丁方案 PRD 增补 2026-10-04）
--
-- 目标：
--   1) dim_variety.continuous_follow_rule —— 888 连续序列实际所跟合约的规则标注。
--      枚举：NEAR_MONTH(近月) / NEXT_MONTH(次月) / TRUE_MAIN(持仓量最大) /
--            FIXED_MONTH(固定月) / CUSTOM(逐品种显式列表)。
--      ★ 默认 NULL = 未标注；执行层遇 NULL 必须显式 ERROR（G1 fail-loud），
--        严禁静默默认（杜绝「未知→有值」类静默失效）。
--   2) dim_main_contract_inferred —— 价格匹配反推的「888 当日实际所跟合约」字典表，
--      是执行反解层（app/execution）的真实合约真源，优先于 main_contract_map.underlying。

-- ---- 1. dim_variety 加列 + 枚举约束 ----
ALTER TABLE dim_variety
    ADD COLUMN IF NOT EXISTS continuous_follow_rule text;

-- 幂等：约束已存在则跳过（本迁移可能随 001-027 一并应用）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_dim_variety_follow_rule') THEN
        ALTER TABLE dim_variety
            ADD CONSTRAINT ck_dim_variety_follow_rule
            CHECK (continuous_follow_rule IS NULL OR continuous_follow_rule IN
                   ('NEAR_MONTH','NEXT_MONTH','TRUE_MAIN','FIXED_MONTH','CUSTOM'));
    END IF;
END $$;

COMMENT ON COLUMN dim_variety.continuous_follow_rule IS
    '888 连续序列实际所跟合约规则（丙/丁方案）：NEAR_MONTH(近月)/NEXT_MONTH(次月)/'
    'TRUE_MAIN(持仓量最大)/FIXED_MONTH(固定月)/CUSTOM(逐品种显式列表)。默认 NULL=未标注，'
    '执行层遇 NULL 必须 ERROR（G1 fail-loud），禁止静默默认。';

-- ---- 2. dim_main_contract_inferred 反推字典表 ----
CREATE TABLE IF NOT EXISTS dim_main_contract_inferred (
    variety_code    text        NOT NULL,
    trade_date      date        NOT NULL,
    inferred_symbol text,
    ref_close       numeric(20,4),
    match_diff      numeric(12,8),
    n_candidates    int,
    method          text,
    mapped_symbol   text,
    agrees_with_map boolean,
    PRIMARY KEY (variety_code, trade_date)
);
CREATE INDEX IF NOT EXISTS ix_dmci_date ON dim_main_contract_inferred(trade_date);

COMMENT ON TABLE dim_main_contract_inferred IS
    '价格匹配反推的「888 当日实际所跟合约」字典（丙/丁方案执行反解层真源）。'
    'inferred_symbol 优先于 main_contract_map.underlying 作为反解真实合约；'
    'agrees_with_map 标记与持仓量主力是否一致，供数据质量面板可观测。';
