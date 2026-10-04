-- 026_futures_rule_table.sql
-- 任务④（PRD §14.4 G4 / C6）：futures_rule 期货专属规则表。
--
-- 来源：akshare `futures_rule(date='YYYYMMDD')`（国泰君安期货，按交易日返回各所各品种
--       交易保证金比例 / 涨跌停板幅度 / 合约乘数 / 最小变动价位 / 限价单最大手数 等）。
-- 用途：
--   * G4：替代 `tool_trade_date_hist_sina`（股票日历）作为期货作息真源；
--     futures_rule 有数据的日期即交易日（非交易日接口抛错，种子脚本据此跳过）。
--   * C6：特殊时段风控窗口（涨跌停扩板 / 保证金调整）直接由 交易保证金比例 / 涨跌停板幅度
--     列供给，无需另建 cfg_risk_window 的静态值。
--
-- 设计：按 (trade_date, exchange, code) 主键，逐日 UPSERT；种子脚本可断点续跑。
CREATE TABLE IF NOT EXISTS futures_rule (
    trade_date      date        NOT NULL,
    exchange        text        NOT NULL,   -- 上期所 / 大商所 / ...
    variety         text,                    -- 铜 / 铝 / ...
    code            text,                    -- CU / AL / ...（期权为 CU_O）
    margin_ratio    numeric(8,4),           -- 交易保证金比例（%）
    price_limit     numeric(8,4),           -- 涨跌停板幅度（%）
    multiplier      numeric(12,4),          -- 合约乘数
    tick_size       numeric(12,6),          -- 最小变动价位
    max_order_lots  int,                    -- 限价单每笔最大下单手数
    special_note    text,                   -- 特殊合约参数调整
    adjust_note     text,                   -- 调整备注
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, exchange, code)
);

COMMENT ON TABLE futures_rule IS
    '期货专属规则表（akshare futures_rule 逐日播种）。替代股票日历作为期货作息真源（G4），'
    '并供给 C6 风控窗口的保证金/涨跌停数据。有数据的日期即交易日。';

CREATE INDEX IF NOT EXISTS ix_futures_rule_date ON futures_rule (trade_date);
CREATE INDEX IF NOT EXISTS ix_futures_rule_code ON futures_rule (code, trade_date);
