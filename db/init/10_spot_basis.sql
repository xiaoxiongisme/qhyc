-- =====================================================
-- PRD §18.5（v1.3.2）M6a：基差因子（spot_basis）
-- 数据源：akshare futures_spot_price_daily(start_day, end_day, vars_list)
--         → DataFrame 13 列（spot_price / near_contract / dominant_contract /
--         near_basis / dom_basis / near_basis_rate / dom_basis_rate）
-- 防前视：T 日数据；T+1 预测用（latency 1 自然生效）
-- 因子：dom_basis_rate（基差率 %）、basis_change_5d、back_month_convention
-- =====================================================
CREATE TABLE IF NOT EXISTS spot_basis (
    report_date           DATE    NOT NULL,    -- 现货数据日期
    exchange              TEXT    NOT NULL DEFAULT 'auto',  -- 品种所属交易所（auto/CZCE/...）
    symbol                TEXT    NOT NULL,    -- 品种简称（c/a/m/y/cu/au...）
    spot_price            NUMERIC(16, 4),     -- 现货价
    near_contract         TEXT,               -- 近期月合约代码
    near_contract_price   NUMERIC(16, 4),
    dominant_contract     TEXT,               -- 主力合约代码
    dominant_contract_price NUMERIC(16, 4),
    near_month            TEXT,               -- 近期月份（yymm）
    dominant_month        TEXT,
    near_basis            NUMERIC(12, 4),     -- 近期月基差（现货-期货）
    dom_basis             NUMERIC(12, 4),     -- 主力月基差
    near_basis_rate       NUMERIC(10, 6),     -- 近期月基差率
    dom_basis_rate        NUMERIC(10, 6),     -- 主力月基差率
    src                   TEXT    NOT NULL DEFAULT 'akshare',
    version               TEXT    NOT NULL DEFAULT 'v1.0',
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (report_date, symbol, version)
);
CREATE INDEX IF NOT EXISTS idx_sb_sym  ON spot_basis (symbol, report_date);
CREATE INDEX IF NOT EXISTS idx_sb_date ON spot_basis (report_date);
