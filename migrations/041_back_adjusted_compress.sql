-- #8 续：对 l2_adj.bar_5m_ba 启用压缩（与源 bar_5m 一致；仅 5m 压缩，15/30/60 不压）。
-- 必须在 040 建表 + 数据写入完成后执行：压缩后再向旧区间 INSERT 会触发重压缩，开销大。

ALTER TABLE l2_adj.bar_5m_ba SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby = 'bucket DESC'
);

-- 压缩 7 天前的 chunk（与源 bar_5m 策略对齐）
SELECT compress_chunk(i)
FROM show_chunks('l2_adj.bar_5m_ba', older_than => interval '7 days') i;

-- 未来自动压缩策略
SELECT add_compression_policy('l2_adj.bar_5m_ba', INTERVAL '7 days');
