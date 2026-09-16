-- =====================================================
-- PRD §18.4（v1.3）M6a：会员持仓排名（龙虎榜）
-- 数据源：akshare get_rank_table_czce / get_shfe_rank_table /
--                    get_cffex_rank_table / get_rank_sum_daily
-- 防前视：T 日盘后数据仅用于 T+1 预测（采集时 15:30 后调度，as_of = T）
-- 覆盖矩阵：CZCE / SHFE / CFFEX（DCE 接口 futures_dce_position_rank 当前坏，
--          降级为 v1.1 后续 akshare 修复后接入）
-- =====================================================

CREATE TABLE IF NOT EXISTS member_position_rank (
    trade_date    DATE          NOT NULL,    -- T 日（盘后）
    exchange      TEXT          NOT NULL,    -- CZCE / SHFE / CFFEX / DCE
    symbol        TEXT          NOT NULL,    -- 合约代码（FG601/CU2603/IF2312）
    member        TEXT          NOT NULL,    -- 期货公司名称（含"代客"等后缀，保留原样）
    rank          INT           NOT NULL,    -- 1~20 排名（4 张表都取前 20）
    long_pos      BIGINT        NOT NULL,    -- 多头持仓量（手）
    short_pos     BIGINT        NOT NULL,    -- 空头持仓量（手）
    long_chg      BIGINT,                   -- 多单日变化
    short_chg     BIGINT,                   -- 空单日变化
    vol_pos       BIGINT,                   -- 总量（多+空 或 总成交排名）
    src           TEXT          NOT NULL DEFAULT 'akshare',
    version       TEXT          NOT NULL DEFAULT 'v1.0',  -- CZCE 2020 前后断点分支（§18.4）
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (trade_date, exchange, symbol, member, version)
);
CREATE INDEX IF NOT EXISTS idx_mpr_sym  ON member_position_rank (symbol, trade_date);
CREATE INDEX IF NOT EXISTS idx_mpr_date ON member_position_rank (trade_date);
CREATE INDEX IF NOT EXISTS idx_mpr_exch ON member_position_rank (exchange, trade_date);
