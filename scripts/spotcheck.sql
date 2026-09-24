\echo '=== RB888 Dec2025 bar_60m mismatches (expect 0) ==='
WITH exp AS (
  SELECT time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
         first(open, ts) o, max(high) h, min(low) l, last(close, ts) c,
         sum(volume)::bigint v, sum(amount) a, last(open_interest, ts) oi
  FROM minute_bar WHERE symbol='RB888' AND ts >= '2025-12-01' AND ts < '2026-01-01'
  GROUP BY 1)
SELECT count(*) AS mismatches FROM (
  SELECT e.bucket FROM exp e JOIN bar_60m b ON b.symbol='RB888' AND b.bucket=e.bucket
  WHERE e.o IS DISTINCT FROM b.open OR e.h IS DISTINCT FROM b.high OR e.l IS DISTINCT FROM b.low
     OR e.c IS DISTINCT FROM b.close OR e.v IS DISTINCT FROM b.volume
     OR e.a IS DISTINCT FROM b.amount OR e.oi IS DISTINCT FROM b.open_interest) x;

\echo '=== RB888 Dec2025 bar_5m mismatches (expect 0) ==='
WITH exp AS (
  SELECT time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
         first(open, ts) o, max(high) h, min(low) l, last(close, ts) c,
         sum(volume)::bigint v, sum(amount) a, last(open_interest, ts) oi
  FROM minute_bar WHERE symbol='RB888' AND ts >= '2025-12-01' AND ts < '2026-01-01'
  GROUP BY 1)
SELECT count(*) AS mismatches FROM (
  SELECT e.bucket FROM exp e JOIN bar_5m b ON b.symbol='RB888' AND b.bucket=e.bucket
  WHERE e.o IS DISTINCT FROM b.open OR e.h IS DISTINCT FROM b.high OR e.l IS DISTINCT FROM b.low
     OR e.c IS DISTINCT FROM b.close OR e.v IS DISTINCT FROM b.volume
     OR e.a IS DISTINCT FROM b.amount OR e.oi IS DISTINCT FROM b.open_interest) x;
