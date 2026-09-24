SET work_mem='512MB';
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'A888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'A888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'A888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'A888' GROUP BY symbol, bucket;
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
FROM minute_bar WHERE symbol = 'B888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'B888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'B888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'B888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'BR888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'BR888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'BR888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'BR888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'CF888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'CF888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'CF888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'CF888' GROUP BY symbol, bucket;
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
FROM minute_bar WHERE symbol = 'EC888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'EC888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'EC888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'EC888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'FB888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'FB888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'FB888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'FB888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'GN888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'GN888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'GN888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'GN888' GROUP BY symbol, bucket;
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
FROM minute_bar WHERE symbol = 'IM888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'IM888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'IM888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'IM888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'JM888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'JM888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'JM888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'JM888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'LC888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'LC888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'LC888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'LC888' GROUP BY symbol, bucket;
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
FROM minute_bar WHERE symbol = 'MA888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'MA888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'MA888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'MA888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'NR888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'NR888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'NR888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'NR888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PB888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PB888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PB888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PB888' GROUP BY symbol, bucket;
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
FROM minute_bar WHERE symbol = 'PR888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PR888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PR888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'PR888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RB888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RB888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RB888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RB888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RO888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RO888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RO888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'RO888' GROUP BY symbol, bucket;
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
FROM minute_bar WHERE symbol = 'SF888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SF888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SF888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SF888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SM888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SM888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SM888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SM888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SR888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SR888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SR888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'SR888' GROUP BY symbol, bucket;
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
FROM minute_bar WHERE symbol = 'TL888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'TL888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'TL888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'TL888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'V888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'V888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'V888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'V888' GROUP BY symbol, bucket;
INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '5 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'WS888' GROUP BY symbol, bucket;
INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '15 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'WS888' GROUP BY symbol, bucket;
INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '30 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'WS888' GROUP BY symbol, bucket;
INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '60 minutes', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = 'WS888' GROUP BY symbol, bucket;
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