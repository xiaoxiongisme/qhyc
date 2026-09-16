-- =====================================================
-- PRD §18.6（v1.3.2）M6b：合约级日线（carry 数据基础）
-- 数据源矩阵（§18.13 同思路）：
--   SINA   futures_zh_daily_sina(合约)     DCE/SHFE/INE/GFEX（大写代码）
--   TQSDK  api.get_kline_serial(合约,86400) CZCE 兜底（sina 对 CZCE Length mismatch）
--   CFFEX  暂不采（金融期货 carry 信号意义弱）
-- 活跃合约清单来源：spot_basis.near_contract / dominant_contract（M6a 已每日维护）
-- 因子：term_slope（期限斜率）/ roll_yield（展期收益）/ term_curv（曲率）
-- =====================================================
CREATE TABLE IF NOT EXISTS contract_daily (
    symbol        TEXT          NOT NULL,    -- 合约代码（统一大写：FG701/cu2610→CU2610）
    product       TEXT          NOT NULL,    -- 品种简称（FG/CU）
    exchange      TEXT          NOT NULL,    -- CZCE/DCE/SHFE/INE/GFEX
    trade_date    DATE          NOT NULL,
    open          NUMERIC(20, 4),
    high          NUMERIC(20, 4),
    low           NUMERIC(20, 4),
    close         NUMERIC(20, 4),
    settle        NUMERIC(20, 4),
    volume        BIGINT,
    oi            BIGINT,                    -- 持仓量（sina: hold / tqsdk: close_oi）
    src           TEXT          NOT NULL DEFAULT 'akshare',  -- akshare / tqsdk
    version       TEXT          NOT NULL DEFAULT 'v1.0',
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, trade_date, version)
);
CREATE INDEX IF NOT EXISTS idx_cd_prod ON contract_daily (product, trade_date);
CREATE INDEX IF NOT EXISTS idx_cd_date ON contract_daily (trade_date);
