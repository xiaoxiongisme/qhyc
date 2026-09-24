SET work_mem='4GB';

INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol,
       time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar GROUP BY symbol, bucket;

INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol,
       time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar GROUP BY symbol, bucket;
