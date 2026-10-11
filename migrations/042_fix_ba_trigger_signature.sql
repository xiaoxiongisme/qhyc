-- #8 续：修复 sp_build_l2_back_adjusted 的全量/增量触发签名。
--
-- 背景（见 CB 工单 2026-10-10）：原签名只比较 MAX(price_shift) 与 price_shift_baseline。
-- 而 10-10 重建 roll_segment 改的是 cum_offset（不是 price_shift），MAX(price_shift)=0 与
-- price_shift_baseline=0 永不 DISTINCT ⇒ 只要不显式传 p_full 就永远走 incremental ⇒
-- bar_*_ba 历史行（含 NaN 行）永不被重算，复权表永久陈旧。
--
-- 修复：把触发签名升级为 (price_shift, Σcum_offset, Σepoch(seg_start), 段数) 四维，任一项
-- 变化（换月日程编辑 / cum_offset 修正 / 分段边界或段数变化）都自动判 full。
-- 同时给 back_adjusted_meta 增加对应基线列并在本迁移中一次性回填。

-- ---------------------------------------------------------------------------
-- 1) meta 增列（幂等）
-- ---------------------------------------------------------------------------
ALTER TABLE l2_adj.back_adjusted_meta
    ADD COLUMN IF NOT EXISTS cum_offset_baseline numeric,
    ADD COLUMN IF NOT EXISTS segstart_baseline  numeric,
    ADD COLUMN IF NOT EXISTS nseg_baseline      integer;

-- ---------------------------------------------------------------------------
-- 2) 重建 procedure（含新签名）
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
    v_cum     numeric;
    v_cum_old numeric;
    v_ss      numeric;
    v_ss_old  numeric;
    v_n       int;
    v_n_old   int;
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

        EXECUTE format('SELECT count(*) FROM l2_adj.roll_segment WHERE symbol=%L AND freq=%L', v_sym, p_freq) INTO v_n;
        IF v_n = 0 THEN
            RAISE NOTICE '[BA] %/% 无 roll_segment 段，跳过', v_sym, p_freq; CONTINUE;
        END IF;

        -- 新签名：price_shift 全局抬升 + Σcum_offset + Σepoch(seg_start) + 段数
        EXECUTE format(
          'SELECT COALESCE(MAX(price_shift),0),
                  COALESCE(SUM(cum_offset),0),
                  COALESCE(SUM(EXTRACT(EPOCH FROM seg_start)::numeric),0),
                  COUNT(*)
           FROM l2_adj.roll_segment WHERE symbol=%L AND freq=%L', v_sym, p_freq)
          INTO v_ps, v_cum, v_ss, v_n;

        EXECUTE format(
          'SELECT max_bucket, price_shift_baseline, cum_offset_baseline, segstart_baseline, nseg_baseline
           FROM l2_adj.back_adjusted_meta WHERE symbol=%L AND freq=%L', v_sym, p_freq)
          INTO v_last, v_ps_old, v_cum_old, v_ss_old, v_n_old;

        IF p_full OR v_last IS NULL
           OR v_ps IS DISTINCT FROM v_ps_old
           OR v_cum IS DISTINCT FROM v_cum_old
           OR v_ss IS DISTINCT FROM v_ss_old
           OR v_n  IS DISTINCT FROM v_n_old THEN
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
                'INSERT INTO l2_adj.back_adjusted_meta (symbol,freq,max_bucket,price_shift_baseline,cum_offset_baseline,segstart_baseline,nseg_baseline,updated_at) '
                'VALUES (%L,%L,(SELECT max(bucket) FROM %s WHERE symbol=%L),%L,%L,%L,%L,now()) '
                'ON CONFLICT (symbol,freq) DO UPDATE SET max_bucket=EXCLUDED.max_bucket, '
                '   price_shift_baseline=EXCLUDED.price_shift_baseline, cum_offset_baseline=EXCLUDED.cum_offset_baseline, '
                '   segstart_baseline=EXCLUDED.segstart_baseline, nseg_baseline=EXCLUDED.nseg_baseline, updated_at=now()',
                v_sym, p_freq, v_dst, v_sym, v_ps, v_cum, v_ss, v_n);
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
    '与 app/data/back_adjust.apply_back_adjust 逐位一致；全量/增量两种模式。'
    '触发签名为 (price_shift, Σcum_offset, Σepoch(seg_start), 段数)，任一项变化自动全量重建（042 修复）。';

-- ---------------------------------------------------------------------------
-- 3) 一次性回填基线（供后续 incremental 正确判定；幂等可重跑）
-- ---------------------------------------------------------------------------
UPDATE l2_adj.back_adjusted_meta m SET
    cum_offset_baseline = COALESCE((SELECT SUM(r.cum_offset)
                                     FROM l2_adj.roll_segment r WHERE r.symbol=m.symbol AND r.freq=m.freq), 0),
    segstart_baseline   = COALESCE((SELECT SUM(EXTRACT(EPOCH FROM r.seg_start)::numeric)
                                     FROM l2_adj.roll_segment r WHERE r.symbol=m.symbol AND r.freq=m.freq), 0),
    nseg_baseline       = COALESCE((SELECT COUNT(*)
                                     FROM l2_adj.roll_segment r WHERE r.symbol=m.symbol AND r.freq=m.freq), 0);
