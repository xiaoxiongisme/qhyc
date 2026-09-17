-- =====================================================
-- 期货预测平台 - 数据库初始化（M1）
-- PRD §4.3 数据模型 / §4.4 向前兼容（小时线预留）
-- TimescaleDB 2.17 + PG16
-- =====================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- -----------------------------------------------------
-- 元数据：品种/合约
-- -----------------------------------------------------
CREATE TABLE IF NOT EXISTS futures_symbol (
    symbol           TEXT        PRIMARY KEY,
    name             TEXT        NOT NULL,
    exchange         TEXT        NOT NULL,           -- CZCE/SHFE/DCE/CFFEX/INE
    unit             TEXT,
    multiplier       NUMERIC(20,4),
    product          TEXT,                            -- 品种代码 (FG/SA/...)
    is_main          BOOLEAN     NOT NULL DEFAULT FALSE, -- 主连合约标记
    main_symbol      TEXT,                            -- 指向所属主连（如 FG888）
    active           BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_futures_symbol_exchange ON futures_symbol (exchange);
CREATE INDEX IF NOT EXISTS idx_futures_symbol_product  ON futures_symbol (product);
CREATE INDEX IF NOT EXISTS idx_futures_symbol_main     ON futures_symbol (main_symbol) WHERE is_main = FALSE;

-- -----------------------------------------------------
-- 主连换月映射（每日主连合约 + 换月标记 + delta）
-- -----------------------------------------------------
CREATE TABLE IF NOT EXISTS main_contract_map (
    trade_date    DATE        NOT NULL,
    exchange      TEXT        NOT NULL,
    product       TEXT        NOT NULL,
    main_symbol   TEXT        NOT NULL,           -- 主连代码，如 FG888
    underlying    TEXT        NOT NULL,           -- 当日实际合约，如 FG605
    change_flag   BOOLEAN     NOT NULL DEFAULT FALSE,
    delta         NUMERIC(20,4) NOT NULL DEFAULT 0,  -- ⑪ 换月拼接价差
    src           TEXT        NOT NULL DEFAULT 'csv', -- csv / akshare / tqsdk
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (trade_date, exchange, product)
);
CREATE INDEX IF NOT EXISTS idx_main_contract_map_product ON main_contract_map (product);
CREATE INDEX IF NOT EXISTS idx_main_contract_map_change  ON main_contract_map (product, change_flag) WHERE change_flag = TRUE;

-- -----------------------------------------------------
-- 日线（TimescaleDB 超表）—— 数据仓库主表（⑪ ⑧）
-- =========================================================
CREATE TABLE IF NOT EXISTS daily_bar (
    symbol      TEXT             NOT NULL,
    trade_date  DATE             NOT NULL,
    open        NUMERIC(20,4),
    high        NUMERIC(20,4),
    low         NUMERIC(20,4),
    close       NUMERIC(20,4),
    settle      NUMERIC(20,4),
    volume      BIGINT,
    amount       NUMERIC(24,4),
    oi          BIGINT,
    ret_close   NUMERIC(12,6),         -- ⑩ 收盘价涨跌幅 %
    ret_settle  NUMERIC(12,6),         -- ⑩ 结算价涨跌幅 %
    ret5        NUMERIC(12,6),
    ret20       NUMERIC(12,6),
    src         TEXT         NOT NULL DEFAULT 'akshare',  -- akshare / tqsdk / csv
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, trade_date)
);
SELECT create_hypertable(
    'daily_bar', 'trade_date',
    chunk_time_interval => INTERVAL '3 months',
    if_not_exists => TRUE
);

-- -----------------------------------------------------
-- 主连表（⑪）：原始主连价 + 平滑主连价
-- =========================================================
CREATE TABLE IF NOT EXISTS main_continuous (
    product        TEXT         NOT NULL,            -- FG / SA ...
    trade_date     DATE         NOT NULL,
    raw_open       NUMERIC(20,4),
    raw_high       NUMERIC(20,4),
    raw_low        NUMERIC(20,4),
    raw_close      NUMERIC(20,4),
    raw_volume     BIGINT,
    raw_oi         BIGINT,
    adj_open       NUMERIC(20,4),                    -- 平滑/复权价
    adj_high       NUMERIC(20,4),
    adj_low        NUMERIC(20,4),
    adj_close      NUMERIC(20,4),
    adj_volume     BIGINT,
    adj_oi         BIGINT,
    underlying     TEXT,                             -- 当日对应合约
    change_flag    BOOLEAN      NOT NULL DEFAULT FALSE,
    src            TEXT         NOT NULL DEFAULT 'csv',
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (product, trade_date)
);
SELECT create_hypertable(
    'main_continuous', 'trade_date',
    chunk_time_interval => INTERVAL '3 months',
    if_not_exists => TRUE
);

-- -----------------------------------------------------
-- 小时线（M6 预留，§4.4 向前兼容）—— M1 不写入
-- =========================================================
CREATE TABLE IF NOT EXISTS hourly_bar (
    symbol          TEXT             NOT NULL,
    trade_datetime  TIMESTAMPTZ      NOT NULL,
    open            NUMERIC(20,4),
    high            NUMERIC(20,4),
    low             NUMERIC(20,4),
    close           NUMERIC(20,4),
    volume          BIGINT,
    oi              BIGINT,
    ret             NUMERIC(12,6),
    src             TEXT         NOT NULL DEFAULT 'akshare',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- G5：同表混存多 src（csv/akshare/tqsdk），靠 src 过滤隔离；主键纳入 src 杜绝「同一根 K 被
    -- 双源各写一次」导致的口径污染（§14 #2）。既有库若已存在重复，用运行时 ensure 的
    -- CREATE UNIQUE INDEX IF NOT EXISTS 兜底（重复行会跳过并告警，不阻断启动）。
    PRIMARY KEY (symbol, trade_datetime, src)
);
SELECT create_hypertable(
    'hourly_bar', 'trade_datetime',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);

-- -----------------------------------------------------
-- 交易日历（含日/夜盘时段，用于缺失检测 §4.3）
-- -----------------------------------------------------
CREATE TABLE IF NOT EXISTS trade_calendar (
    exchange   TEXT        NOT NULL,
    trade_date DATE        NOT NULL,
    is_open    BOOLEAN     NOT NULL DEFAULT TRUE,
    sessions   JSONB,                              -- [{start:"09:00",end:"10:15"},...]
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (exchange, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_trade_calendar_open ON trade_calendar (trade_date) WHERE is_open = TRUE;

-- -----------------------------------------------------
-- 预测结果（§5.3 输出契约 v1）
-- -----------------------------------------------------
CREATE TABLE IF NOT EXISTS prediction_result (
    run_id            TEXT        NOT NULL,
    symbol            TEXT        NOT NULL,
    target_date       DATE        NOT NULL,
    as_of_date        DATE        NOT NULL,
    as_of_ts          TIMESTAMPTZ NOT NULL,
    model_set         TEXT        NOT NULL DEFAULT 'm2',     -- m2/m3 等
    direction         TEXT,                                    -- up / down
    direction_prob    NUMERIC(6,4),
    ret_point         NUMERIC(12,6),
    ret_low           NUMERIC(12,6),
    ret_high          NUMERIC(12,6),
    confidence        NUMERIC(6,4),
    state             TEXT,                                    -- trend / mean_revert
    participated_models JSONB,
    schema_version    INT         NOT NULL DEFAULT 1,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_prediction_symbol_target ON prediction_result (symbol, target_date);
CREATE INDEX IF NOT EXISTS idx_prediction_target_date  ON prediction_result (target_date);

-- -----------------------------------------------------
-- 回测结果（§6）
-- -----------------------------------------------------
CREATE TABLE IF NOT EXISTS backtest_result (
    run_id         TEXT         NOT NULL,
    model          TEXT         NOT NULL,
    window_len     INT          NOT NULL DEFAULT 250,   -- 注意：window 为 PG 保留字，故用 window_len
    start_date     DATE,
    end_date       DATE,
    dir_acc        NUMERIC(6,4),
    mae            NUMERIC(12,6),
    rmse           NUMERIC(12,6),
    quantile_hit   NUMERIC(6,4),
    sample_n       INT,
    by_state       JSONB,                              -- 按 Hurst 状态分层
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, model)
);

-- -----------------------------------------------------
-- 异常工单（§4.1 校准）
-- -----------------------------------------------------
CREATE TABLE IF NOT EXISTS anomaly_ticket (
    id            BIGSERIAL    PRIMARY KEY,
    symbol        TEXT         NOT NULL,
    trade_date    DATE         NOT NULL,
    field         TEXT         NOT NULL,             -- close / settle / volume ...
    akshare_val   NUMERIC(20,4),
    tqsdk_val     NUMERIC(20,4),
    diff          NUMERIC(12,6),                     -- 绝对偏差（%）
    threshold     NUMERIC(12,6),
    status        TEXT         NOT NULL DEFAULT 'pending', -- pending / accepted / fixed / false_positive
    note          TEXT,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    resolved_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_anomaly_pending ON anomaly_ticket (status) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_anomaly_symbol  ON anomaly_ticket (symbol, trade_date);

-- -----------------------------------------------------
-- 简报引擎信号入库（§13，对接预留）
-- -----------------------------------------------------
CREATE TABLE IF NOT EXISTS briefing_signal (
    id           BIGSERIAL    PRIMARY KEY,
    source       TEXT         NOT NULL,
    symbol       TEXT         NOT NULL,
    trade_date   DATE         NOT NULL,
    direction    TEXT,                                -- up/down/neutral
    score        NUMERIC(6,4),
    note         TEXT,
    received_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_briefing_signal_symbol_date ON briefing_signal (symbol, trade_date);

-- -----------------------------------------------------
-- 采集/校准任务流水（用于看板 /tasks/{id}）
-- -----------------------------------------------------
CREATE TABLE IF NOT EXISTS task_run (
    id            BIGSERIAL    PRIMARY KEY,
    task_type     TEXT         NOT NULL,        -- ingest / calibrate / predict / backtest / import
    label         TEXT,                          -- noon/close/night/manual
    status        TEXT         NOT NULL DEFAULT 'running', -- running / success / failed
    payload       JSONB,
    started_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    finished_at   TIMESTAMPTZ,
    message       TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_run_type_status ON task_run (task_type, status);
CREATE INDEX IF NOT EXISTS idx_task_run_started    ON task_run (started_at DESC);

-- -----------------------------------------------------
-- 压缩策略（§4.4）
-- -----------------------------------------------------
ALTER TABLE daily_bar SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby = 'trade_date DESC'
);
SELECT add_compression_policy('daily_bar', INTERVAL '30 days', if_not_exists => TRUE);

ALTER TABLE main_continuous SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'product',
    timescaledb.compress_orderby = 'trade_date DESC'
);
SELECT add_compression_policy('main_continuous', INTERVAL '30 days', if_not_exists => TRUE);

ALTER TABLE hourly_bar SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby = 'trade_datetime DESC'
);
SELECT add_compression_policy('hourly_bar', INTERVAL '7 days', if_not_exists => TRUE);

-- -----------------------------------------------------
-- 连续聚合：小时线 -> 日线（M6 启用，先建好）
-- =========================================================
CREATE MATERIALIZED VIEW IF NOT EXISTS hourly_1d_rollup
WITH (timescaledb.continuous) AS
SELECT
    symbol,
    time_bucket('1 day', trade_datetime) AS bucket,
    FIRST(open, trade_datetime)   AS open,
    MAX(high)                     AS high,
    MIN(low)                      AS low,
    LAST(close, trade_datetime)   AS close,
    SUM(volume)                   AS volume,
    SUM(oi)                       AS oi
FROM hourly_bar
GROUP BY symbol, bucket
WITH NO DATA;
SELECT add_continuous_aggregate_policy('hourly_1d_rollup',
    start_offset => INTERVAL '7 days',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 day',
    if_not_exists => TRUE);

-- =====================================================
-- End of M1 schema
-- =====================================================