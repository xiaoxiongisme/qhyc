-- =============================================================================
-- 081：BA 孤儿桶防复发（CL-20261115-04）
--
-- 症状：purge_recent_buckets 删 l1_mkt.bar_* 最近 4 天旧桶并按会话网格重建；
--   l2_adj.bar_*m_ba 由本过程增量刷新（INSERT ... ON CONFLICT，从不删桶），
--   L1 已不存在的 bucket 因此永久留在 BA。BAR-GRID-VALID 实测 11-09 有 57 根、
--   11-14 有 2 根（SS888/AO888 @ 00:10）。V5X 直接读 BA，幽灵桶会占状态机桶位。
--
-- 为什么放在过程里：全仓 4 个 BA 写入点（app/ingest/close_pipeline.py:551、
--   app/ops/task_registry.py:313、app/scheduler.py:3465、
--   scripts/ops/rebuild_roll_all_freqs.py:120）全部 CALL 本过程。改任一 Python
--   调用点都只能覆盖一条路径 —— 会重演「写了没人读」的隐蔽失效。
--
-- ⚠ 基线纪律：本过程体以 **2026-11-15 现网 pg_get_functiondef 导出的 prosrc
--   逐字为基线**（128 行），不是 migrations/066 文件。065/066/063 三次事故都是
--   「拿旧迁移当基线 CREATE OR REPLACE，把后续改动静默回退」。生成脚本会校验
--   除本段新增代码外与现网逐字一致，MIG-PROC-BASELINE 四个 token 全部保留。
--
-- 安全边界：14 天窗口 + NOT EXISTS 精确匹配；不在 purge 里删 BA（不可逆丢数据）；
--   只在真的删到孤儿时才回落 meta，正常路径零额外开销。
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
    v_purged  int := 0;
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

    -- ---- CL-20261115-04：BA 孤儿桶防复发（每次刷新，所有调用方统一生效） ----
    -- 背景：purge_recent_buckets 删 L1 四表旧网格桶并重建，而本过程的
    --   full / incremental 分支都只 INSERT ... ON CONFLICT，从不 DELETE 已有桶，
    --   于是 L1 已不存在的 bucket 会永久留在 l2_adj.bar_*m_ba（V5X 唯一信号源）。
    --   BAR-GRID-VALID docstring 实证 11-09 曾一次出现 57 根幽灵桶。
    -- 判据：只删「近 14 天 + L1 无同 (symbol,bucket)」的行。窗口与
    --   BAR-GRID-VALID 的检测窗口一致（断言看得见的孤儿一定被覆盖），
    --   14 天 >> purge 的 4 天删除范围，留足重建余量。
    -- 幂等/安全：NOT EXISTS 精确匹配，绝不碰 L1 仍存在的桶；集合级一次执行，
    --   还能覆盖「L1 里该 symbol 已完全消失」的极端情形（逐 symbol 循环做不到）。
    -- ⚠ 不放进 purge_recent_buckets：BA 由本过程增量刷新，不在 L1 的同事务
    --   重建链里；若在 purge 里同步删 BA，会不可逆丢数据。
    EXECUTE format(
        'DELETE FROM %s a '
        'WHERE a.bucket >= now() - interval ''14 days'' '
        '  AND NOT EXISTS (SELECT 1 FROM %s l '
        '                  WHERE l.symbol = a.symbol AND l.bucket = a.bucket)',
        v_dst, v_src);
    GET DIAGNOSTICS v_purged = ROW_COUNT;
    IF v_purged > 0 THEN
        -- meta.max_bucket 取自 BA 自身 max(bucket)（见下方两个分支）。孤儿若
        -- 落在最新端会把它带高，incremental 分支的 `>= v_last` 便跳过 L1 真实桶，
        -- 使 DATA-L2-FRESH 反而报红。清理后同步回落，保证本轮循环能补回。
        EXECUTE format(
            'UPDATE l2_adj.back_adjusted_meta m '
            'SET max_bucket = (SELECT max(bucket) FROM %s d WHERE d.symbol = m.symbol) '
            'WHERE m.freq = %L '
            '  AND m.max_bucket IS DISTINCT FROM '
            '      (SELECT max(bucket) FROM %s d WHERE d.symbol = m.symbol)',
            v_dst, p_freq, v_dst);
        RAISE NOTICE '[BA-ORPHAN-GUARD] % 清理孤儿 % 根（CL-20261115-04）',
                     p_freq, v_purged;
    END IF;

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
    '066：065 误用 040 作基线，本条迁移以 053 为基线重建，恢复口径与四维签名。'
    '081 / CL-20261115-04：每轮刷新前精确清理近 14 天 BA 孤儿桶（L1 无对应），'
    '并在清到孤儿时回落 back_adjusted_meta.max_bucket，防 incremental 跳过补桶。';

COMMIT;
