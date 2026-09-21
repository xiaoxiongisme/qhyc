-- =====================================================
-- PRD §18.4（v1.3）M6a：会员持仓排名（龙虎榜）
-- 数据源（2026-09-20 统一为**交易所官方源**，见 app/ingest/member_position.py 矩阵）：
--   CZCE  ak.get_rank_table_czce          官方全量（只取合约级，丢品种级「合计」键）
--   SHFE  ak.get_shfe_rank_table          官方全量（约 74 合约；绝不传 vars_list）
--   GFEX  ak.futures_gfex_position_rank   官方（SI/LC/PS，2023-11-10 上市起）
--   DCE   app.ingest.dce_scrapling        Scrapling 真浏览器绕大商所瑞数动态防护
--   INE   无免费源 → 不纳入；CFFEX 金融期货（决策 3）→ 不纳入
-- 覆盖范围为各所**当日全量合约**（不再按 config.main_contracts 限品种）。
-- 防前视：T 日盘后数据仅用于 T+1 预测（调度 17:30，as_of = T）
-- 说明：单会员行按维度（成交量/多单持仓/空单持仓）分别填列，其余方向取 NULL
--
-- ⚠️ symbol 口径（2026-09-20 统一）：**标准码 = 品种大写 + YYMM 四位**，如 AP2701 / CU2611。
--    各所官方原生写法不统一（CZCE 是 3 位 AP701、SHFE/DCE/GFEX 小写 cu2611），
--    而新浪源统一 4 位（AP2701）——曾造成同一合约两套 symbol、跨源 join 全对不上。
--    入库前一律经 app.core.symbol_code.to_std() 归一；映射见表 contract_code_map
--    （DDL: db/init/14_contract_code.sql）。
-- =====================================================

CREATE TABLE IF NOT EXISTS member_position_rank (
    trade_date    DATE          NOT NULL,    -- T 日（盘后）
    exchange      TEXT          NOT NULL,    -- CZCE / SHFE / DCE / GFEX / INE（商品期货；CFFEX 不纳入）
    symbol        TEXT          NOT NULL,    -- 合约代码（标准码 4 位，如 AP2701 / CU2611）
    member        TEXT          NOT NULL,    -- 期货公司名称（含"代客"等后缀，保留原样）
    rank          INT           NOT NULL,    -- 1~20 排名（合约级；合计行已丢）
    long_pos      BIGINT,                   -- 多头持仓量（手）
    short_pos     BIGINT,                   -- 空头持仓量（手）
    long_chg      BIGINT,                   -- 多单日变化
    short_chg     BIGINT,                   -- 空单日变化
    vol_pos       BIGINT,                   -- 成交量（手）
    src           TEXT          NOT NULL DEFAULT 'sina_cot',
    version       TEXT          NOT NULL DEFAULT 'v1.0',
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (trade_date, exchange, symbol, member, version)
);
CREATE INDEX IF NOT EXISTS idx_mpr_sym  ON member_position_rank (symbol, trade_date);
CREATE INDEX IF NOT EXISTS idx_mpr_date ON member_position_rank (trade_date);
CREATE INDEX IF NOT EXISTS idx_mpr_exch ON member_position_rank (exchange, trade_date);
