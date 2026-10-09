-- #8 后复权物化落地：把「raw + roll_segment.cum_offset 的 as-of join」物化为 L2 表，
-- 让回测 / 信号取数直接读已复权 OHLC，消除 Python 端 pull-raw + 逐根 as-of 的成本。
--
-- 口径：与 app/data/back_adjust.apply_back_adjust 逐位一致
--       （已用 RB888/min15 验收：adj_close / adj_offset 的 max|diff| = 0.000000）。
--       后复权价 = raw + (cum_offset + price_shift)；real = adj - adj_offset（下单前反解）。
--
-- 设计要点
-- ------
-- * 每周期一张物化表 l2_adj.bar_{5,15,30,60}m_ba，由 l1_mkt.bar_*m 派生（L2 = 派生/复权层）。
-- * chunk 间隔与源表对齐（5m=30d / 15m,30m=90d / 60m=180d）。
-- * build 过程 sp_build_l2_back_adjusted 单 symbol 内循环，支持 全量 / 增量 两种模式：
--     - 全量：该 symbol 在 ba 表 DELETE 后整段重建（price_shift 基线变化或首次）。
--     - 增量：仅补 bucket > meta.max_bucket 的新 bar（后复权历史稳定，append 即可）。
--   price_shift 是整序列常量抬升；若 roll_segment 的基线变化（极少，仅长 backwardation 品种
--   出新段把全局 min 推更低），自动降级为全量重建，保证历史一致。
-- * 仅 bar_5m_ba 启用压缩（与源 bar_5m 一致）；压缩在 041 中建完数据后开启。
-- * 幂等：table / hypertable / procedure 均 IF NOT EXISTS / CREATE OR REPLACE。

CREATE SCHEMA IF NOT EXISTS l2_adj;

-- ---------------------------------------------------------------------------
-- 物化表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS l2_adj.bar_5m_ba (
    symbol        TEXT        NOT NULL,
    bucket        TIMESTAMPTZ NOT NULL,
    open          NUMERIC,
    high          NUMERIC,
    low           NUMERIC,
    close         NUMERIC,
    volume        BIGINT,
    amount        NUMERIC,
    open_interest NUMERIC,
    adj_offset    NUMERIC,    -- 套用偏移 = cum_offset + price_shift；real = adj - adj_offset
    PRIMARY KEY (symbol, bucket)
);
CREATE TABLE IF NOT EXISTS l2_adj.bar_15m_ba (
    symbol        TEXT        NOT NULL,
    bucket        TIMESTAMPTZ NOT NULL,
    open          NUMERIC, high NUMERIC, low NUMERIC, close NUMERIC,
    volume        BIGINT, amount NUMERIC, open_interest NUMERIC,
    adj_offset    NUMERIC,
    PRIMARY KEY (symbol, bucket)
);
CREATE TABLE IF NOT EXISTS l2_adj.bar_30m_ba (
    symbol        TEXT        NOT NULL,
    bucket        TIMESTAMPTZ NOT NULL,
    open          NUMERIC, high NUMERIC, low NUMERIC, close NUMERIC,
    volume        BIGINT, amount NUMERIC, open_interest NUMERIC,
    adj_offset    NUMERIC,
    PRIMARY KEY (symbol, bucket)
);
CREATE TABLE IF NOT EXISTS l2_adj.bar_60m_ba (
    symbol        TEXT        NOT NULL,
    bucket        TIMESTAMPTZ NOT NULL,
    open          NUMERIC, high NUMERIC, low NUMERIC, close NUMERIC,
    volume        BIGINT, amount NUMERIC, open_interest NUMERIC,
    adj_offset    NUMERIC,
    PRIMARY KEY (symbol, bucket)
);

-- 时间维度索引（与源 bar_*m_bucket_idx 对齐，支撑非 symbol 前缀的时间范围扫描）
CREATE INDEX IF NOT EXISTS bar_5m_ba_bucket_idx  ON l2_adj.bar_5m_ba  (bucket DESC);
CREATE INDEX IF NOT EXISTS bar_15m_ba_bucket_idx ON l2_adj.bar_15m_ba (bucket DESC);
CREATE INDEX IF NOT EXISTS bar_30m_ba_bucket_idx ON l2_adj.bar_30m_ba (bucket DESC);
CREATE INDEX IF NOT EXISTS bar_60m_ba_bucket_idx ON l2_adj.bar_60m_ba (bucket DESC);

-- ---------------------------------------------------------------------------
-- 转为 hypertable（chunk 间隔与源对齐）
-- ---------------------------------------------------------------------------
SELECT create_hypertable('l2_adj.bar_5m_ba',  'bucket', chunk_time_interval => interval '30 days',  if_not_exists => true);
SELECT create_hypertable('l2_adj.bar_15m_ba', 'bucket', chunk_time_interval => interval '90 days',  if_not_exists => true);
SELECT create_hypertable('l2_adj.bar_30m_ba', 'bucket', chunk_time_interval => interval '90 days',  if_not_exists => true);
SELECT create_hypertable('l2_adj.bar_60m_ba', 'bucket', chunk_time_interval => interval '180 days', if_not_exists => true);

-- ---------------------------------------------------------------------------
-- 构建进度 / 基线追踪
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS l2_adj.back_adjusted_meta (
    symbol               TEXT        NOT NULL,
    freq                 TEXT        NOT NULL,
    max_bucket           TIMESTAMPTZ,
    price_shift_baseline NUMERIC,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, freq)
);

-- ---------------------------------------------------------------------------
-- 构建过程：raw + roll_segment 的 as-of join 物化
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE l2_adj.sp_build_l2_back_adjusted(
    p_freq   text DEFAULT 'min15',
    p_symbol text DEFAULT NULL,
    p_full   boolean DEFAULT false)
LANGUAGE plpgsql AS $$
DECLARE
    v_src  text := CASE p_freq WHEN 'min5'  THEN 'l1_mkt.bar_5m'
                               WHEN 'min15' THEN 'l1_mkt.bar_15m'
                               WHEN 'min30' THEN 'l1_mkt.bar_30m'
                               WHEN 'min60' THEN 'l1_mkt.bar_60m' END;
    v_dst  text := CASE p_freq WHEN 'min5'  THEN 'l2_adj.bar_5m_ba'
                               WHEN 'min15' THEN 'l2_adj.bar_15m_ba'
                               WHEN 'min30' THEN 'l2_adj.bar_30m_ba'
                               WHEN 'min60' THEN 'l2_adj.bar_60m_ba' END;
    v_sym     text;
    v_last    timestamptz;
    v_ps      numeric;
    v_ps_old  numeric;
    v_nseg    int;
    v_mode    text;
    v_ins     int;
    v_asof    text;
BEGIN
    IF v_src IS NULL OR v_dst IS NULL THEN
        RAISE EXCEPTION 'sp_build_l2_back_adjusted: 不支持的 freq=%', p_freq;
    END IF;
    IF to_regclass('l2_adj.roll_segment') IS NULL THEN
        RAISE EXCEPTION 'sp_build_l2_back_adjusted: roll_segment 不存在，拒绝执行';
    END IF;
    -- 复用的 as-of join 片段（LEFT JOIN LATERAL + COALESCE 兜底早于首段的 bar）
    v_asof := format(
        'LEFT JOIN LATERAL ('
        '  SELECT COALESCE((r.cum_offset + COALESCE(r.price_shift,0)),0) AS off '
        '  FROM l2_adj.roll_segment r '
        '  WHERE r.symbol=b.symbol AND r.freq=%1$L AND r.seg_start <= b.bucket '
        '  ORDER BY r.seg_start DESC LIMIT 1) off ON true', p_freq);

    FOR v_sym IN
        EXECUTE format('SELECT DISTINCT symbol FROM %s WHERE symbol ~ ''^[A-Za-z]+888$'' ORDER BY symbol', v_src)
    LOOP
        IF p_symbol IS NOT NULL AND v_sym <> p_symbol THEN CONTINUE; END IF;

        EXECUTE format('SELECT count(*) FROM l2_adj.roll_segment WHERE symbol=%L AND freq=%L', v_sym, p_freq) INTO v_nseg;
        IF v_nseg = 0 THEN
            RAISE NOTICE '[BA] %/% 无 roll_segment 段，跳过', v_sym, p_freq; CONTINUE;
        END IF;

        EXECUTE format('SELECT COALESCE(MAX(price_shift),0) FROM l2_adj.roll_segment WHERE symbol=%L AND freq=%L', v_sym, p_freq) INTO v_ps;
        EXECUTE format('SELECT max_bucket, price_shift_baseline FROM l2_adj.back_adjusted_meta WHERE symbol=%L AND freq=%L', v_sym, p_freq) INTO v_last, v_ps_old;

        IF p_full OR v_last IS NULL OR v_ps IS DISTINCT FROM v_ps_old THEN
            v_mode := 'full';
        ELSE
            v_mode := 'incremental';
        END IF;

        IF v_mode = 'full' THEN
            EXECUTE format('DELETE FROM %s WHERE symbol=%L', v_dst, v_sym);
            EXECUTE format(
                'INSERT INTO %s (symbol,bucket,open,high,low,close,volume,amount,open_interest,adj_offset) '
                'SELECT b.symbol,b.bucket, b.open+off.off, b.high+off.off, b.low+off.off, b.close+off.off, '
                '       b.volume, b.amount, b.open_interest, off.off '
                'FROM %s b %s WHERE b.symbol=%L '
                'ON CONFLICT (symbol,bucket) DO UPDATE SET '
                '   open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close, '
                '   volume=EXCLUDED.volume, amount=EXCLUDED.amount, open_interest=EXCLUDED.open_interest, adj_offset=EXCLUDED.adj_offset',
                v_dst, v_src, v_asof, v_sym);
            GET DIAGNOSTICS v_ins = ROW_COUNT;
            EXECUTE format(
                'INSERT INTO l2_adj.back_adjusted_meta (symbol,freq,max_bucket,price_shift_baseline,updated_at) '
                'VALUES (%L,%L,(SELECT max(bucket) FROM %s WHERE symbol=%L),%L,now()) '
                'ON CONFLICT (symbol,freq) DO UPDATE SET max_bucket=EXCLUDED.max_bucket, price_shift_baseline=EXCLUDED.price_shift_baseline, updated_at=now()',
                v_sym, p_freq, v_dst, v_sym, v_ps);
        ELSE
            EXECUTE format(
                'INSERT INTO %s (symbol,bucket,open,high,low,close,volume,amount,open_interest,adj_offset) '
                'SELECT b.symbol,b.bucket, b.open+off.off, b.high+off.off, b.low+off.off, b.close+off.off, '
                '       b.volume, b.amount, b.open_interest, off.off '
                'FROM %s b %s WHERE b.symbol=%L AND b.bucket > %L::timestamptz '
                'ON CONFLICT (symbol,bucket) DO UPDATE SET '
                '   open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close, '
                '   volume=EXCLUDED.volume, amount=EXCLUDED.amount, open_interest=EXCLUDED.open_interest, adj_offset=EXCLUDED.adj_offset',
                v_dst, v_src, v_asof, v_sym, v_last);
            GET DIAGNOSTICS v_ins = ROW_COUNT;
            EXECUTE format(
                'UPDATE l2_adj.back_adjusted_meta SET max_bucket=(SELECT max(bucket) FROM %s WHERE symbol=%L), updated_at=now() WHERE symbol=%L AND freq=%L',
                v_dst, v_sym, v_sym, p_freq);
        END IF;
        RAISE NOTICE '[BA] %/% % ins=%s', v_sym, p_freq, v_mode, v_ins;
    END LOOP;
END;
$$;

COMMENT ON PROCEDURE l2_adj.sp_build_l2_back_adjusted IS
    '#8 后复权物化：raw+roll_segment 的 as-of join 写入 l2_adj.bar_*m_ba；'
    '与 app/data/back_adjust.apply_back_adjust 逐位一致；全量/增量两种模式，price_shift 变化自动全量重建。';
