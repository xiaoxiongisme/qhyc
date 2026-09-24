SET work_mem='512MB';
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'AL8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'AL8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'AL8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'AL8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'AU8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'AU8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'AU8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'AU8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'BC8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'BC8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'BC8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'BC8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'C8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'C8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'C8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'C8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'CS8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'CS8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'CS8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'CS8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'EB8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'EB8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'EB8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'EB8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'ER8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'ER8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'ER8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'ER8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'FU8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'FU8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'FU8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'FU8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'I8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'I8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'I8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'I8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'IH8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'IH8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'IH8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'IH8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'JD8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'JD8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'JD8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'JD8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'L8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'L8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'L8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'L8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'LH8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'LH8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'LH8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'LH8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'M8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'M8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'M8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'M8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'NI8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'NI8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'NI8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'NI8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'P8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'P8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'P8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'P8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PG8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PG8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PG8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PG8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PP8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PP8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PP8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PP8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PX8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PX8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PX8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PX8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RM8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RM8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RM8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RM8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RS8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RS8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RS8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RS8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SC8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SC8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SC8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SC8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SI8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SI8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SI8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SI8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SP8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SP8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SP8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SP8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'T8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'T8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'T8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'T8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'TF8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'TF8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'TF8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'TF8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'UR8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'UR8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'UR8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'UR8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'WR8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'WR8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'WR8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'WR8888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'Y8888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'Y8888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'Y8888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'Y8888' GROUP BY symbol, bucket;