\echo '=== distinct symbols: minute_bar vs each bar table ==='
SELECT (SELECT count(DISTINCT symbol) FROM minute_bar) AS mb_syms,
       (SELECT count(DISTINCT symbol) FROM bar_5m)  AS b5,
       (SELECT count(DISTINCT symbol) FROM bar_15m) AS b15,
       (SELECT count(DISTINCT symbol) FROM bar_30m) AS b30,
       (SELECT count(DISTINCT symbol) FROM bar_60m) AS b60;

\echo '=== symbols in minute_bar missing from a bar table ==='
SELECT 'bar_5m' t, symbol FROM (SELECT DISTINCT symbol FROM minute_bar) s
 WHERE symbol NOT IN (SELECT symbol FROM bar_5m)
UNION ALL
SELECT 'bar_60m' t, symbol FROM (SELECT DISTINCT symbol FROM minute_bar) s
 WHERE symbol NOT IN (SELECT symbol FROM bar_60m);

\echo '=== count mismatches: expected buckets (recomputed) vs stored, per table (limit 20) ==='
WITH exp AS (SELECT symbol, count(*) n FROM (SELECT DISTINCT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') b FROM minute_bar) z GROUP BY symbol)
SELECT 'bar_5m' t, e.symbol, e.n expected, COALESCE(g.n,0) got
FROM exp e LEFT JOIN (SELECT symbol, count(*) n FROM bar_5m GROUP BY symbol) g USING(symbol)
WHERE e.n <> COALESCE(g.n,0) LIMIT 20;

WITH exp AS (SELECT symbol, count(*) n FROM (SELECT DISTINCT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') b FROM minute_bar) z GROUP BY symbol)
SELECT 'bar_15m' t, e.symbol, e.n expected, COALESCE(g.n,0) got
FROM exp e LEFT JOIN (SELECT symbol, count(*) n FROM bar_15m GROUP BY symbol) g USING(symbol)
WHERE e.n <> COALESCE(g.n,0) LIMIT 20;

WITH exp AS (SELECT symbol, count(*) n FROM (SELECT DISTINCT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') b FROM minute_bar) z GROUP BY symbol)
SELECT 'bar_30m' t, e.symbol, e.n expected, COALESCE(g.n,0) got
FROM exp e LEFT JOIN (SELECT symbol, count(*) n FROM bar_30m GROUP BY symbol) g USING(symbol)
WHERE e.n <> COALESCE(g.n,0) LIMIT 20;

WITH exp AS (SELECT symbol, count(*) n FROM (SELECT DISTINCT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') b FROM minute_bar) z GROUP BY symbol)
SELECT 'bar_60m' t, e.symbol, e.n expected, COALESCE(g.n,0) got
FROM exp e LEFT JOIN (SELECT symbol, count(*) n FROM bar_60m GROUP BY symbol) g USING(symbol)
WHERE e.n <> COALESCE(g.n,0) LIMIT 20;

\echo '=== max bucket per table (should reach 2025-12-31) ==='
SELECT 'b5' t, max(bucket) FROM bar_5m
UNION ALL SELECT 'b15', max(bucket) FROM bar_15m
UNION ALL SELECT 'b30', max(bucket) FROM bar_30m
UNION ALL SELECT 'b60', max(bucket) FROM bar_60m;

\echo '=== NULL OHLC check per table ==='
SELECT 'b5' t, count(*) AS nulls FROM bar_5m WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL
UNION ALL SELECT 'b15', count(*) FROM bar_15m WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL
UNION ALL SELECT 'b30', count(*) FROM bar_30m WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL
UNION ALL SELECT 'b60', count(*) FROM bar_60m WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL;
