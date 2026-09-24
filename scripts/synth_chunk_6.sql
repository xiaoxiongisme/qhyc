SET work_mem='1GB';
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'AO888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'AO888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'AO888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'AO888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'BB888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'BB888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'BB888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'BB888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'C888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'C888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'C888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'C888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'CU888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'CU888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'CU888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'CU888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'EG888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'EG888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'EG888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'EG888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'FU888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'FU888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'FU888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'FU888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'IC888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'IC888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'IC888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'IC888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'J888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'J888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'J888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'J888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'L888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'L888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'L888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'L888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'LR888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'LR888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'LR888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'LR888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'ME888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'ME888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'ME888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'ME888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'P888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'P888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'P888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'P888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PK888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PK888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PK888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PK888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PS888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PS888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PS888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PS888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RM888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RM888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RM888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RM888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RU888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RU888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RU888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RU888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SH888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SH888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SH888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SH888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SP888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SP888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SP888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SP888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'TA888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'TA888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'TA888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'TA888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'TS888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'TS888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'TS888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'TS888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'WR888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'WR888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'WR888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'WR888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'ZC888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'ZC888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'ZC888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'ZC888' GROUP BY symbol, bucket;