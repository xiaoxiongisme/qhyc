-- 076_enable_bar_compression.sql
-- CL-20261111-14：把 6 张从未纳入压缩体系的 bar hypertable 补齐配置 + 策略
--
-- 根因（2026-11-12 全系统"看不见的问题"审计）
-- ------------------------------------------
-- 压缩作业**本身完全正常**：crontab 每日 03:30 调 run_compress.sh，日志
-- 2026-11-11T19:30Z OK，存量 l0_raw.minute_bar / l1_mkt.bar_5m 已被压缩。
-- 真正缺口是这 6 张表压根没进压缩体系：
--   · l1_mkt.bar_15m / bar_30m / bar_60m
--   · l2_adj.bar_15m_ba / bar_30m_ba / bar_60m_ba
-- 三者皆 compression_enabled=f、无 add_compression_policy、且
-- scripts/db/compress_existing_chunks.sql 的目标列表只有 minute_bar + bar_5m。
-- 实测合计 5.4 GB（1953+1071+629+942+597+306 MB），占 31 GB 库的 17%。
-- 作业记 success、磁盘慢慢涨、没有任何断言发现 —— 典型「看不见」。
--
-- 配置模板（照抄已压的 l1_mkt.bar_5m / l2_adj.bar_5m_ba）
-- ------------------------------------------------------
-- timescaledb_information.compression_settings 实测：
--   segmentby = symbol（index 1）
--   orderby   = bucket DESC NULLS FIRST（orderby_asc=f, nullsfirst=t）
-- TSDB 版本 2.17.1。
--
-- ⚠ 压缩窗口取 30 天（与 l1_mkt.bar_5m 一致），比 l2_adj.bar_5m_ba 的 7 天
--   更保守：l2_adj.bar_*_ba 会被 sp_build_l2_back_adjusted 重建，留足近期
--   可写窗口，避免压缩 chunk 撞上后复权重算。
-- ⚠ 本迁移只改元数据 + 注册策略，**不压缩任何 chunk**（重 IO 留给
--   compress_existing_chunks.sql 由 03:30 低峰 cron 执行），可在盘中安全执行。
--
-- 回滚
-- ----
-- 先 remove_compression_policy，再 decompress_chunk 逐块解压（可逆、不丢数据），
-- 最后 ALTER TABLE ... RESET (timescaledb.compress)。解压是必须的前置：
-- 带已压缩 chunk 的表不能直接关压缩。
--
BEGIN;

-- ① 开启压缩配置（幂等：已开启的表跳过）
DO $$
DECLARE
    t text;
    targets text[] := ARRAY[
        'l1_mkt.bar_15m', 'l1_mkt.bar_30m', 'l1_mkt.bar_60m',
        'l2_adj.bar_15m_ba', 'l2_adj.bar_30m_ba', 'l2_adj.bar_60m_ba'
    ];
BEGIN
    FOREACH t IN ARRAY targets LOOP
        IF NOT EXISTS (
            SELECT 1 FROM timescaledb_information.hypertables h
            WHERE format('%I.%I', h.hypertable_schema, h.hypertable_name) = t
              AND h.compression_enabled
        ) THEN
            EXECUTE format(
                'ALTER TABLE %s SET (timescaledb.compress, '
                'timescaledb.compress_segmentby = ''symbol'', '
                'timescaledb.compress_orderby = ''bucket DESC NULLS FIRST'')', t);
            RAISE NOTICE 'enabled compression on %', t;
        ELSE
            RAISE NOTICE 'compression already enabled on % (skip)', t;
        END IF;
    END LOOP;
END $$;

-- ② 注册 30 天压缩策略（if_not_exists => TRUE 保证幂等）
SELECT add_compression_policy('l1_mkt.bar_15m',    INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_compression_policy('l1_mkt.bar_30m',    INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_compression_policy('l1_mkt.bar_60m',    INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_compression_policy('l2_adj.bar_15m_ba', INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_compression_policy('l2_adj.bar_30m_ba', INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_compression_policy('l2_adj.bar_60m_ba', INTERVAL '30 days', if_not_exists => TRUE);

COMMIT;

-- 验收（迁移执行后单独跑）：
--   SELECT hypertable_schema, hypertable_name, compression_enabled
--   FROM timescaledb_information.hypertables
--   WHERE hypertable_name IN ('bar_15m','bar_30m','bar_60m',
--                             'bar_15m_ba','bar_30m_ba','bar_60m_ba');
