SET work_mem='4GB';
\echo '=== 9 non-positive price rows (symbol,ts,ohlc) ==='
SELECT symbol, ts, open, high, low, close, volume FROM minute_bar WHERE open<=0 OR high<=0 OR low<=0 OR close<=0 ORDER BY ts;

\echo '=== bar_* null ohlc ==='
SELECT 'bar_5m' t, count(*) FILTER (WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL) FROM bar_5m
UNION ALL SELECT 'bar_15m', count(*) FILTER (WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL) FROM bar_15m
UNION ALL SELECT 'bar_30m', count(*) FILTER (WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL) FROM bar_30m
UNION ALL SELECT 'bar_60m', count(*) FILTER (WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL) FROM bar_60m;

\echo '=== minute_bar rows by year (scope of 2005-2014 deletion) ==='
SELECT extract(year FROM ts)::int AS yr, count(*) FROM minute_bar GROUP BY 1 ORDER BY 1;
