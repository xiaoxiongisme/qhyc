\echo '=== hypertables ==='
SELECT hypertable_name, num_chunks, (SELECT range_end FROM timescaledb_information.chunks c WHERE c.hypertable_name=h.hypertable_name ORDER BY range_end DESC LIMIT 1) AS newest_chunk_end
FROM timescaledb_information.hypertables h ORDER BY 1;

\echo '=== table list (public) ==='
SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY 1;

\echo '=== per-table time range ==='
SELECT 'minute_bar' t, min(ts), max(ts) FROM minute_bar
UNION ALL SELECT 'bar_5m', min(bucket), max(bucket) FROM bar_5m
UNION ALL SELECT 'bar_15m', min(bucket), max(bucket) FROM bar_15m
UNION ALL SELECT 'bar_30m', min(bucket), max(bucket) FROM bar_30m
UNION ALL SELECT 'bar_60m', min(bucket), max(bucket) FROM bar_60m;

\echo '=== minute_bar columns ==='
SELECT column_name, data_type FROM information_schema.columns WHERE table_name='minute_bar' ORDER BY ordinal_position;
