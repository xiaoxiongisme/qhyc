-- =====================================================
-- PRD §18.5（v1.3）M6a：库存 / 仓单
-- 数据源：akshare futures_inventory_em(symbol) / get_receipt(start,end,vars_list) /
--                    futures_*_warehouse_receipt(date) （按交易所分）
-- 周频：交易收盘后采集 16:30，report_date 通常为 T 日
-- 因子：库存环比/同比；与 carry 高度相关，与价格特征真正正交（§18.5）
-- =====================================================

CREATE TABLE IF NOT EXISTS inventory (
    report_date   DATE          NOT NULL,    -- 数据日期
    exchange      TEXT          NOT NULL,    -- DCE / SHFE / CZCE / INE / GFEX
    symbol        TEXT          NOT NULL,    -- 品种（a/m/y/c 等简称或合约号）
    warehouse     TEXT          NOT NULL DEFAULT '',  -- 仓库名（em 接口无仓库，记空串）
    inventory_qty BIGINT,                    -- 库存量（手/吨，em 接口为张/吨）
    receipt_qty   BIGINT,                    -- 仓单量（可空）
    unit          TEXT,                      -- 单位（手/张/吨）
    change_qty    BIGINT,                    -- 日变化（环比）
    src           TEXT          NOT NULL DEFAULT 'akshare',
    version       TEXT          NOT NULL DEFAULT 'v1.0',
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (report_date, exchange, symbol, warehouse, version)
);
CREATE INDEX IF NOT EXISTS idx_inv_sym  ON inventory (symbol, report_date);
CREATE INDEX IF NOT EXISTS idx_inv_date ON inventory (report_date);
CREATE INDEX IF NOT EXISTS idx_inv_exch ON inventory (exchange, report_date);
