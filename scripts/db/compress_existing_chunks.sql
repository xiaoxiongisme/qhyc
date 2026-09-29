-- 存量 chunk 手工压缩（评估文档 7.1 / P0#1）
-- 仅压缩「已落出」压缩策略窗口的历史存量 chunk；与 add_compression_policy 互补：
-- 策略只对未来落出窗口的 chunk 生效，本脚本处理历史存量。
-- if_not_compressed => true 保证可重复、安全（已压缩的自动跳过）。
-- 重 IO/CPU 操作：请在低峰（夜盘后、04:30 adjust_minute_bar_adj 之前）由 cron 触发，
-- 切勿在交易/采集时段手动跑。
SELECT compress_chunk(i, if_not_compressed => true)
FROM show_chunks('fut_kline', older_than => INTERVAL '90 days') i;

SELECT compress_chunk(i, if_not_compressed => true)
FROM show_chunks('minute_bar', older_than => INTERVAL '30 days') i;

SELECT compress_chunk(i, if_not_compressed => true)
FROM show_chunks('bar_5m', older_than => INTERVAL '60 days') i;
