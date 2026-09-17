-- =====================================================
-- PRD §18.4（v1.3）M6a：会员持仓排名（龙虎榜）
-- 数据源：新浪财经期货成交持仓（akshare futures_hold_pos_sina + match_main_contract，
--   见 app/ingest/rank_position.py）。仅商品期货；金融期货（CFFEX）不纳入、不补（决策 3）。
--   逐合约→主力合约映射自 config.main_contracts，仅入库本项目覆盖品种。
-- 防前视：T 日盘后数据仅用于 T+1 预测（调度 17:30，as_of = T）
-- 说明：单会员行按维度（成交量/多单持仓/空单持仓）分别填列，其余方向取 NULL
-- =====================================================

CREATE TABLE IF NOT EXISTS member_position_rank (
    trade_date    DATE          NOT NULL,    -- T 日（盘后）
    exchange      TEXT          NOT NULL,    -- CZCE / SHFE / DCE / GFEX / INE（商品期货；CFFEX 不纳入）
    symbol        TEXT          NOT NULL,    -- 主力合约代码（如 RB2510）
    member        TEXT          NOT NULL,    -- 期货公司名称（含"代客"等后缀，保留原样）
    rank          INT           NOT NULL,    -- 1~20 排名（三维度各取前 20，按会员名合并）
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
