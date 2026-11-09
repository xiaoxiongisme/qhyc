-- =============================================================================
-- CL-20261109-12：增量重建把「最后一个桶」也刷新（> → >=）
--
-- 症状：全量重建若发生在盘中（或任何桶还没走完的时刻），ba 里最后一根是**半成品**；
--       之后该桶在 l1 侧被补全，但增量分支只写 `bucket > max_bucket`，永远跳过它
--       → 这根 bar 永久保留错误的收盘价（2026-11-09 11:10 实测 A888 差 1 点、
--       BR888 差 35 点），而 adj_offset 是对的，整条链路不报错。
--
-- 修法：增量下界由 `>` 改为 `>=`，每次重算最后一个已知桶。代价是每个
--       (symbol,freq) 每轮多写 1 行（ON CONFLICT DO UPDATE 已具备），可忽略。
--
-- ⚠ 边界：`>=` 只能自愈「最后一根」，历史中段的修订仍要靠全量重建
--   （`rebuild_roll_all_freqs.py --force-ba`）。两者配套使用，
--   并由此派生断言 `BA-PRICE-IDENTITY`（45 天窗口，排除当日未收盘桶）。
-- =============================================================================

BEGIN;

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
                'FROM %s b %s WHERE b.symbol=%L AND b.bucket >= %L::timestamptz '
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


COMMENT ON PROCEDURE l2_adj.sp_build_l2_back_adjusted(text, text, boolean) IS
    '后复权物化：raw + roll_segment 的 as-of join 写 l2_adj.bar_*m_ba。'
    'CL-20261109-12：增量下界改为 >=，每轮刷新最后一个已知桶，避免盘中全量重建'
    '留下的半成品 bar 永久冻结。历史中段修订仍需 --force-ba 全量重建。';

COMMIT;
