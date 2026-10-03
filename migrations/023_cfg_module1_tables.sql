-- 023_cfg_module1_tables.sql
-- PRD §3 模块①：配置管理（cfg / 字典）—— 把写死在代码/env 里的配置收口到数据库。
--
-- 三张表（PRD §3.1）：
--   cfg_trading_session  交易时间段（日盘/夜盘起止）—— 夜盘归属与 K 线对齐的唯一权威
--   cfg_holiday          休市/节假日日历
--   cfg_symbol_margin    保证金（仅保证金；合约乘数/tick 已在 dim_variety，不重复建列）
--
-- ⚠ 注意 PRD §3.4 / C8：**不要新建 cfg_symbol_fee**（手续费表）。
--   手续费唯一真源是既有的 dim_trading_cost（迁移 013/014/019/020/021）。

CREATE TABLE IF NOT EXISTS cfg_trading_session (
    id             bigserial PRIMARY KEY,
    exchange       text NOT NULL,
    variety_code   text,                      -- NULL = 适用该所全部品种
    session_type   text NOT NULL,             -- 'day' | 'night'
    start_time     time NOT NULL,
    end_time       time NOT NULL,
    next_day_flag  boolean NOT NULL DEFAULT false,  -- 结束时间是否跨到次日
    is_active      boolean NOT NULL DEFAULT true,
    source         text,
    note           text,
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- ⚠ 唯一键必须含 start_time：一个品种的日盘有 3 段（09:00-10:15 / 10:30-11:30 /
--   13:30-15:00），若只按 (exchange, variety, session_type) 唯一，后面的段会被
--   ON CONFLICT DO NOTHING 静默丢弃，导致下午盘缺失。
DROP INDEX IF EXISTS uq_cfg_trading_session;
CREATE UNIQUE INDEX IF NOT EXISTS uq_cfg_trading_session
    ON cfg_trading_session (exchange, COALESCE(variety_code, '*'), session_type, start_time);

COMMENT ON TABLE cfg_trading_session IS
    '交易时间段字典（PRD §3 模块①）。夜盘归属规则：hour >= 夜盘开始 的 K 线归属下一交易日。';

-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cfg_holiday (
    trade_date      date PRIMARY KEY,
    is_trading_day  boolean NOT NULL,
    exchange        text,                     -- NULL = 全市场
    note            text,
    source          text,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE cfg_holiday IS
    '休市/节假日日历（PRD §3 模块①）。用于跳过非交易日、判定"下一交易日"。'
    '⚠ 期货作息与股票不同（G4），日历须来自期货专属源。';

CREATE INDEX IF NOT EXISTS ix_cfg_holiday_exchange ON cfg_holiday (exchange, trade_date);

-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cfg_symbol_margin (
    variety_code          text PRIMARY KEY,
    exchange              text,
    exchange_margin_ratio numeric(10, 6),     -- 交易所基准（D1 段）
    broker_margin_ratio   numeric(10, 6),     -- 期货公司（= 交易所 + 上浮）
    margin_d2             numeric(10, 6),     -- D2 段（连续同方向停板次日）
    margin_d3             numeric(10, 6),     -- D3 段
    source                text,
    note                  text,
    updated_at            timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE cfg_symbol_margin IS
    '保证金字典（PRD §3 模块①）。合约乘数/tick 仍在 dim_variety，此处不重复。'
    'D1/D2/D3 = 连续同方向涨跌停板各时段的交易所保证金分段。';

-- 保证金不得为负或 > 1（百分比误写成小数倍是常见错误）
ALTER TABLE cfg_symbol_margin
    DROP CONSTRAINT IF EXISTS ck_cfg_symbol_margin_range;
ALTER TABLE cfg_symbol_margin
    ADD CONSTRAINT ck_cfg_symbol_margin_range
    CHECK (exchange_margin_ratio IS NULL
           OR (exchange_margin_ratio > 0 AND exchange_margin_ratio <= 1));
