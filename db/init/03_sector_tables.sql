-- =====================================================
-- §16.5 数据层新增（PRD v1.2）：跨品种/跨大类传导
-- =====================================================

-- 品种分类与产业链角色（§16.1）
CREATE TABLE IF NOT EXISTS sector_map (
    product     TEXT        PRIMARY KEY,
    sector      TEXT        NOT NULL,              -- black/energy_chem/agri/nonferrous/precious/financial
    chain_role  TEXT,                              -- upstream/midstream/downstream/none
    active      BOOLEAN     NOT NULL DEFAULT TRUE, -- 纳入预测范围
    src         TEXT        NOT NULL DEFAULT 'yaml',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sector_map_sector ON sector_map (sector);

-- 大类指数（合成数据，无需 tqsdk 校验，§16.1）
CREATE TABLE IF NOT EXISTS sector_index (
    sector          TEXT         NOT NULL,
    trade_date      DATE         NOT NULL,
    ret_1d          NUMERIC(12,6),                    -- 当日板块收益 %
    ret_5d          NUMERIC(12,6),                    -- 5 日累计动量 %
    ret_20d         NUMERIC(12,6),                    -- 20 日累计动量 %
    index_level     NUMERIC(20,6),                    -- 指数水平（基期 1000）
    weight_method   TEXT         NOT NULL DEFAULT 'volume',  -- amount(缺)/volume/equal
    composition     JSONB,                            -- 每日成分留痕 {product: weight}
    PRIMARY KEY (sector, trade_date)
);
SELECT create_hypertable('sector_index', 'trade_date', if_not_exists => TRUE);

-- 传导权重矩阵（§16.1/16.5）：src→dst，method=prior/corr/granger/te/var，每周重算（与 LSTM 周训对齐）
CREATE TABLE IF NOT EXISTS transmission_weights (
    src_product   TEXT         NOT NULL,
    dst_product   TEXT         NOT NULL,
    method        TEXT         NOT NULL,             -- prior/corr/granger/te/var/blend
    direction     TEXT         NOT NULL DEFAULT 'positive',  -- positive/negative
    lag_days      INT          NOT NULL DEFAULT 1,   -- 0/1/5 档
    weight        NUMERIC(8,4) NOT NULL,             -- 强度档 0-1（先验）或统计量归一
    cost_ratio    NUMERIC(6,4),                       -- 成本占比参考（先验）
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (src_product, dst_product, method, lag_days)
);
CREATE INDEX IF NOT EXISTS idx_transmission_dst ON transmission_weights (dst_product, method);
