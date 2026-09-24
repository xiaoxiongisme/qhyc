\echo '=== DB size ==='
SELECT pg_size_pretty(pg_database_size('futures')) AS db_size;

\echo '=== minute_bar dirty checks ==='
SELECT
  count(*) FILTER (WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL) AS null_ohlc,
  count(*) FILTER (WHERE high < low) AS high_lt_low,
  count(*) FILTER (WHERE open<=0 OR high<=0 OR low<=0 OR close<=0) AS nonpos,
  count(*) FILTER (WHERE volume<0) AS neg_vol,
  count(*) AS total
FROM minute_bar;

\echo '=== duplicate (symbol,ts) in minute_bar ==='
SELECT count(*) AS dup_rows FROM (SELECT symbol, ts FROM minute_bar GROUP BY 1,2 HAVING count(*)>1) x;

\echo '=== minute_bar rows by year (sizes the 2005-2014 deletion) ==='
SELECT extract(year FROM ts)::int AS yr, count(*) FROM minute_bar GROUP BY 1 ORDER BY 1;

\echo '=== bar_* null ohlc ==='
SELECT 'bar_5m' t, count(*) FILTER (WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL) FROM bar_5m
UNION ALL SELECT 'bar_15m', count(*) FILTER (WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL) FROM bar_15m
UNION ALL SELECT 'bar_30m', count(*) FILTER (WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL) FROM bar_30m
UNION ALL SELECT 'bar_60m', count(*) FILTER (WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL) FROM bar_60m;
