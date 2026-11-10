-- 存量 chunk 手工压缩（评估文档 7.1 / P0#1）
-- 仅压缩「已落出」压缩策略窗口的历史存量 chunk；与 add_compression_policy 互补：
-- 策略只对未来落出窗口的 chunk 生效，本脚本处理历史存量。
-- if_not_compressed => true 保证可重复、安全（已压缩的自动跳过）。
-- 重 IO/CPU 操作：请在低峰（夜盘后、04:30 adjust_minute_bar_adj 之前）由 cron 触发，
-- 切勿在交易/采集时段手动跑。
--
-- ★ 2026-11-10（CL-20261110-10 C）：修复三条 show_chunks 的裸表名。
--   qhyc-timescaledb 的 search_path = "$user", public，而 13 张 hypertable 全在
--   l0_raw / l1_mkt / l2_adj；裸名会解析到 public 兼容视图，报
--   "is not a hypertable or a continuous aggregate"，fut_kline 更是早已不存在。
--   psql -f 默认不 ON_ERROR_STOP，三条全失败仍可能退 0 → task_runner 记 success 假绿
--   （与 cron 的 exit 126 叠加，使这个作业长期「看起来在跑、其实什么都没压」）。
--   带 schema 实测：show_chunks('l0_raw.minute_bar', >30d)=279、
--   show_chunks('l1_mkt.bar_5m', >60d)=265，均可正常解析。
--   fut_kline 为已退役表，其日线存量由 policy_compression[1000] 对 l0_raw.daily_bar
--   承担，故不再单列压缩目标（该表已不存在，留着只会每天报错）。
SELECT compress_chunk(i, if_not_compressed => true)
FROM show_chunks('l0_raw.minute_bar', older_than => INTERVAL '30 days') i;

SELECT compress_chunk(i, if_not_compressed => true)
FROM show_chunks('l1_mkt.bar_5m', older_than => INTERVAL '60 days') i;
