-- futures-data-fetch 技能适配层：统一行情超表
-- 频率(freq) × 类型(kind) × 品种/合约(symbol) × 时间(trade_datetime)
--   kind: continuous(主连 KQ.m@) / contract(具体合约 EX.CODE) / cont_adj(复权主连)
-- 注：实际建表/建超表由 app.ingest.fdf.db_pg.ensure_schema 在运行时幂等完成；
-- 本文件作为迁移留档，可在容器初始化时手动执行。

CREATE TABLE IF NOT EXISTS fut_kline (
    freq            TEXT        NOT NULL,
    kind            TEXT        NOT NULL,
    symbol          TEXT        NOT NULL,
    trade_datetime  TIMESTAMPTZ NOT NULL,
    open            NUMERIC(20,4),
    high            NUMERIC(20,4),
    low             NUMERIC(20,4),
    close           NUMERIC(20,4),
    volume          BIGINT,
    oi              BIGINT,
    adj             NUMERIC(20,4) DEFAULT 0,
    PRIMARY KEY (freq, kind, symbol, trade_datetime)
);


DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM timescaledb_information.hypertables
    WHERE hypertable_name = 'fut_kline'
  ) THEN
    PERFORM create_hypertable('fut_kline', 'trade_datetime');
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_fut_kline_lookup
  ON fut_kline (freq, kind, symbol, trade_datetime);
