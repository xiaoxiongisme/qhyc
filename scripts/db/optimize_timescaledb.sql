-- ============================================================================
-- QHYC TimescaleDB 优化脚本（评估文档 §7.1 / P0#1、P0#2）
--
-- 用法：
--   psql "$DATABASE_URL" -f scripts/db/optimize_timescaledb.sql
--   或经容器：
--   docker exec -i qhyc-timescaledb psql -U futures futures -f - < scripts/db/optimize_timescaledb.sql
--
-- ⚠️ 安全执行建议：
--   * set_chunk_time_interval / CREATE INDEX CONCURRENTLY / add_*_policy 均为在线操作，
--     可在低峰期直接执行，不阻塞读写（索引用 CONCURRENTLY）。
--   * 对「存量 chunk」的手工压缩（文件末「维护窗口」段）会逐 chunk 加短暂锁并占用大量
--     IO/CPU，请在业务低峰（如夜盘后 03:00-05:00）分批执行，避免影响采集/信号链路。
-- ============================================================================

-- ---------- 1. Chunk 间隔调整（P0#1 根因：fut_kline 1133 chunks）----------
-- 当前 fut_kline 约 1133 个 chunk（间隔过小）。改为按月（30 天）≈ 120 chunks，
-- 显著降低超表元数据开销与全表扫描时的 chunk 打开成本。
SELECT set_chunk_time_interval('fut_kline', INTERVAL '30 days');

-- minute_bar（137 chunks）/ bar_5m（135 chunks）当前 chunk 数合理，保持。

-- ---------- 2. 压缩策略（P0#1：降低 60-80% 磁盘、提升查询 3-10x）----------
ALTER TABLE fut_kline SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol,freq,kind',
    timescaledb.compress_orderby = 'trade_datetime DESC'
);
SELECT add_compression_policy('fut_kline', INTERVAL '90 days');

ALTER TABLE minute_bar SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby = 'trade_datetime DESC'
);
SELECT add_compression_policy('minute_bar', INTERVAL '30 days');

ALTER TABLE bar_5m SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby = 'bucket DESC'
);
SELECT add_compression_policy('bar_5m', INTERVAL '60 days');

-- ---------- 3. 索引优化（P0#2：消除 factor_value 全表扫描）----------
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_factor_value_date_symbol
    ON factor_value(trade_date, symbol) INCLUDE (raw_value, z_value);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_factor_value_fid_date
    ON factor_value(factor_id, trade_date DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_mpr_symbol_date
    ON member_position_rank(symbol, trade_date DESC);

-- ---------- 4. 慢查询追踪（可选，需 shared_preload_libraries 含 pg_stat_statements）----------
-- 若未启用，CREATE EXTENSION 会报错；请在 postgresql.conf 加入后重试。
-- CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- ---------- 5. 连续聚合（可选增强，非紧急；见评估文档 §7.1.4）----------
-- CREATE MATERIALIZED VIEW IF NOT EXISTS bar_15m_daily
-- WITH (timescaledb.continuous) AS
-- SELECT symbol, time_bucket('1 day', bucket) AS day,
--        first(open, bucket) AS open, max(high) AS high,
--        min(low) AS low, last(close, bucket) AS close, sum(volume) AS volume
-- FROM bar_15m GROUP BY symbol, time_bucket('1 day', bucket);
-- SELECT add_continuous_aggregate_policy('bar_15m_daily',
--     start_offset => INTERVAL '7 days', end_offset => INTERVAL '1 hour',
--     schedule_interval => INTERVAL '1 hour');

-- ============================ 维护窗口（手工压缩存量）============================
-- 压缩策略只对未来落出窗口的 chunk 生效，不回压存量。需在低峰分批执行，例如：
-- SELECT compress_chunk(i, if_not_compressed => true)
-- FROM show_chunks('fut_kline', older_than => INTERVAL '90 days') i;
-- SELECT compress_chunk(i, if_not_compressed => true)
-- FROM show_chunks('minute_bar', older_than => INTERVAL '30 days') i;
-- SELECT compress_chunk(i, if_not_compressed => true)
-- FROM show_chunks('bar_5m', older_than => INTERVAL '60 days') i;
