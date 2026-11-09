-- =============================================================================
-- 066：修复 065 的「基线取错」回归 —— 恢复 053 的品种口径 + 保留 >= 增量下界
--
-- 事故（CL-20261109-13，同日第二次犯同型错误）：
--   migrations/065 我是拿 **040** 的过程体去改的，只动了 `>` → `>=` 一处，
--   结果把 **053** 里加的 `l3_ref.tradable_scope_array()` 品种口径（S3 收口）、
--   以及 042 引入的四维触发签名（price_shift / Σcum_offset / Σepoch(seg_start) / 段数）
--   一起覆盖掉了。
--
--   证据（065 应用后线上 prosrc）：
--     position('tradable_scope_array' ...) = 0
--     position('cum_offset_baseline'  ...) = 0
--     length(prosrc) = 4808   -- 比 053 版短 1/3
--   后果：L2 后复权重新对**全部** 888 主连做 as-of 复权（含 44 个非白名单品种），
--   `VAR-SCOPE-WIRED` 判红「表建了没人读」。数据本身没错（口径是收窄而非放宽），
--   但白名单纪律被绕过，且非白名单品种会被持续派生。
--
--   同型前例：migrations/063 基于 055 而非 056 重建，把 056 的
--   positive_shift 短路覆盖掉了，当天上午刚修过一次。
--
-- 本迁移：以 **053 的过程体为基线**（逐字复制），唯一实质改动是增量下界
--   `b.bucket > %L::timestamptz` → `b.bucket >= %L::timestamptz`（065 的本意）。
--
-- 安全性：恢复四维签名不会引发误判全量重建 —— 已核对 back_adjusted_meta 的
--   三个 baseline 列与当前 roll_segment 签名逐品种一致（sig_mismatch = 0 / 4 频），
--   因此下一轮走向 incremental，不会在收盘后突然全量重建。
--
-- 防复发：guard_invariants 新增 `MIG-PROC-BASELINE` 断言，对「有过多次重定义」
--   的关键过程做 token 白名单校验（见同 commit 的 guard_invariants.py）。
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
    v_cum     numeric;
    v_cum_old numeric;
    v_ss      numeric;
    v_ss_old  numeric;
    v_n       int;
    v_n_old   int;
    v_mode    text;
    v_ins     int;
    v_asof    text;
    v_scope     text[];
    v_scope_on  boolean := false;
    v_done    int := 0;
BEGIN
    IF v_src IS NULL OR v_dst IS NULL THEN
        RAISE EXCEPTION 'sp_build_l2_back_adjusted: 不支持的 freq=%', p_freq;
    END IF;
    IF to_regclass('l2_adj.roll_segment') IS NULL THEN
        RAISE EXCEPTION 'sp_build_l2_back_adjusted: roll_segment 不存在，拒绝执行';
    END IF;

    -- ---- 品种口径（CL-20261107-01 S3） ----
    v_scope := l3_ref.tradable_scope_array();
    IF v_scope IS NULL THEN
        v_scope_on := false;
        RAISE WARNING '[BA] 口径视图不可用/为空 → 本轮全品种复权（fail-open）';
    ELSE
        v_scope_on := true;
        RAISE NOTICE '[BA] 品种口径生效：% 个 symbol', cardinality(v_scope);
    END IF;

    v_asof := format(
        'LEFT JOIN LATERAL ('
        '  SELECT COALESCE((r.cum_offset + COALESCE(r.price_shift,0)),0) AS off '
        '  FROM l2_adj.roll_segment r '
        '  WHERE r.symbol=b.symbol AND r.freq=%1$L AND r.seg_start <= b.bucket '
        '  ORDER BY r.seg_start DESC LIMIT 1) off ON true', p_freq);

    FOR v_sym IN
        -- $1=正则 $2=口径开关 $3=口径数组；开关关闭或数组为空时不做过滤
        EXECUTE format(
            'SELECT DISTINCT symbol FROM %s WHERE symbol ~ $1 '
            'AND (NOT $2 OR symbol = ANY($3)) ORDER BY symbol', v_src)
        USING '^[A-Za-z]+888$', v_scope_on, v_scope
    LOOP
        IF p_symbol IS NOT NULL AND v_sym <> p_symbol THEN CONTINUE; END IF;

        EXECUTE format('SELECT count(*) FROM l2_adj.roll_segment WHERE symbol=%L AND freq=%L', v_sym, p_freq) INTO v_n;
        IF v_n = 0 THEN
            RAISE NOTICE '[BA] %/% 无 roll_segment 段，跳过', v_sym, p_freq; CONTINUE;
        END IF;

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
        v_done := v_done + 1;
        RAISE NOTICE '[BA] %/% % ins=%s', v_sym, p_freq, v_mode, v_ins;
    END LOOP;
    RAISE NOTICE '[BA] %/% 完成：处理 % 个品种（口径开关=%）', p_freq, p_freq, v_done, v_scope_on;
END;
$$;

COMMENT ON PROCEDURE l2_adj.sp_build_l2_back_adjusted(text, text, boolean) IS
    '#8 后复权物化：raw + roll_segment 的 as-of join → l2_adj.bar_*m_ba。'
    '触发签名 (price_shift, Σcum_offset, Σepoch(seg_start), 段数) 决定 full/incremental（042）。'
    'CL-20261107-01 S3：仅处理「白名单 ∪ 持仓豁免」品种，口径读 '
    'l3_ref.tradable_scope_array()，不可用时 fail-open 全量复权。'
    'CL-20261109-12（065 本意）：增量下界由 > 改为 >=，每轮刷新最后一个已知桶，'
    '避免盘中全量重建留下的半成品 bar 永久冻结；历史中段修订仍需 --force-ba。'
    '066：065 误用 040 作基线，本条迁移以 053 为基线重建，恢复口径与四维签名。';

COMMIT;
