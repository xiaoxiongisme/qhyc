-- =============================================================================
-- 009 · L0 → L1 → L2 存储过程
-- 日期：2026-09-30　数据层重构（用户要求：L1/L2 通过存储过程由 L0 产生，只增改不删）
-- 适用：本地库与云端库同构执行（幂等）
--
-- 分层（依据：数据层复权架构裁定_20260930 + 用户 2026-09-30 拍板「1分钟缺失，其余周期采集补充」）
--   L0 = 直接采集的原始棒(5/15/30/60m/daily) + 基本面原始表（只入库、备份、不删）
--   L1 = 由 L0 归一化出的 888/8888 棒(bar_*)（只增改不删）
--   L2 = 由 L1 产生的换月 offset（roll_segment，只追加不删）
--
-- 提供的过程
--   sp_build_l1_from_minute(p_freq)   L0(1m) → L1(Xm) 经典 time_bucket 聚合（当前 1m 缺失→安全 no-op）
--   sp_normalize_l1_symbols()        L1 符号审计：报告 bar_* 中无法经 dim_symbol 解析为 888/8888 的符号
--   sp_refresh_dim_contract_dates()  由 contract_daily 回填 dim_contract 生命周期（只增改）
--   sp_build_l2_roll_segment(p_freq) L1 → L2 换月 offset（append-only，移植双门检测；**需云端 A/B 验证**）
--
-- ⚠ 验证要求（无本地 DB 可连，本迁移只保证 DDL 合法，逻辑需在云端 dry-run）
--   sp_build_l2_roll_segment 必须经 scripts/build_roll_segments.py 双门结果 A/B 对拍，
--   通过后再退役 Python（保留为校验基准）。L2 为 append-only，错误运行只会产生需复核的段，
--   可用 DELETE FROM roll_segment WHERE symbol=? AND freq=? AND seg_start > 已知正确末段 安全回退。
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 辅助：freq → (bar 表, roll_segment.freq 代码)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION l1_table_of(p_freq text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE p_freq
        WHEN 'min5'  THEN 'bar_5m'
        WHEN 'min15' THEN 'bar_15m'
        WHEN 'min30' THEN 'bar_30m'
        WHEN 'min60' THEN 'bar_60m'
        ELSE NULL END;
$$;

-- ---------------------------------------------------------------------------
-- sp_build_l1_from_minute：经典 L0(1m) → L1(Xm) 聚合
--   当未来 1 分钟数据补回时，本过程即可由此重建各周期 L1 棒。
--   当前 minute_bar 为空 → 安全 no-op（不报错、不写）。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE sp_build_l1_from_minute(p_freq text DEFAULT 'min15')
LANGUAGE plpgsql AS $$
DECLARE
    v_table text := l1_table_of(p_freq);
    v_src   text := 'minute_bar';
    v_src_n bigint;
    v_dst_n bigint;
    v_interval text := CASE p_freq
        WHEN 'min5'  THEN '5 minutes'
        WHEN 'min15' THEN '15 minutes'
        WHEN 'min30' THEN '30 minutes'
        WHEN 'min60' THEN '60 minutes'
        ELSE NULL END;
BEGIN
    IF v_table IS NULL OR v_interval IS NULL THEN
        RAISE EXCEPTION 'sp_build_l1_from_minute: 不支持的 freq=%（仅 min5/15/30/60）', p_freq;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename=v_src) THEN
        RAISE NOTICE '[L1] 源表 % 不存在，跳过', v_src; RETURN;
    END IF;
    EXECUTE format('SELECT count(*) FROM %I', v_src) INTO v_src_n;
    IF v_src_n = 0 THEN
        RAISE NOTICE '[L1] 源表 % 为 0 行（1分钟数据缺失），跳过（安全 no-op）', v_src;
        RETURN;
    END IF;

    -- ⚠ 目标列名必须与 bar_* 真实 schema 一致：load_1min.py 定义的是 open_interest（非 oi）
    EXECUTE format($q$
        INSERT INTO %I (symbol, bucket, open, high, low, close, volume, open_interest)
        SELECT symbol,
               time_bucket($1, ts) AS bucket,
               first(open  ORDER BY ts) AS open,
               max(high)               AS high,
               min(low)                AS low,
               last(close ORDER BY ts) AS close,
               sum(volume)             AS volume,
               max(open_interest)      AS open_interest
        FROM minute_bar
        WHERE symbol ~ '^[A-Za-z]+8888?$'          -- 仅主连/指数连
        GROUP BY symbol, time_bucket($1, ts)
        ON CONFLICT (symbol, bucket) DO UPDATE
           SET open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
               close=EXCLUDED.close, volume=EXCLUDED.volume, open_interest=EXCLUDED.open_interest
        $q$, v_table)
        USING (v_interval::interval);

    EXECUTE format('SELECT count(*) FROM %I', v_table) INTO v_dst_n;
    RAISE NOTICE '[L1] % ← minute_bar 聚合完成（目标表现有 % 行）', v_table, v_dst_n;
END;
$$;

-- ---------------------------------------------------------------------------
-- sp_normalize_l1_symbols：L1 符号审计（只读，不改动）
--   报告 bar_* 中无法经 dim_symbol 解析为 888/8888 首选符号的行，便于发现脏命名空间。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE sp_normalize_l1_symbols()
LANGUAGE plpgsql AS $$
DECLARE
    r record;
    v_bad bigint;
BEGIN
    FOR r IN SELECT unnest(ARRAY['bar_5m','bar_15m','bar_30m','bar_60m']) AS t LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename=r.t) THEN
            CONTINUE;
        END IF;
        EXECUTE format($q$
            SELECT count(*) FROM %I b
            WHERE NOT EXISTS (
                SELECT 1 FROM dim_symbol d
                WHERE d.symbol = b.symbol AND d.namespace IN ('main','index'))
              AND b.symbol !~ '^[A-Za-z]+8888?$'
            $q$, r.t) INTO v_bad;
        IF v_bad > 0 THEN
            RAISE WARNING '[L1] % 存在 % 行符号非 888/8888 且不在 dim_symbol（需人工核对）', r.t, v_bad;
        ELSE
            RAISE NOTICE '[L1] % 符号全部为 888/8888 规范命名', r.t;
        END IF;
    END LOOP;
END;
$$;

-- ---------------------------------------------------------------------------
-- sp_refresh_dim_contract_dates：由 contract_daily 回填生命周期（只增改）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE sp_refresh_dim_contract_dates()
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='contract_daily') THEN
        RAISE NOTICE '[dim] contract_daily 不存在，跳过'; RETURN;
    END IF;
    UPDATE dim_contract dc
       SET list_date        = COALESCE(dc.list_date, s.first_td),
           first_trade_date = COALESCE(dc.first_trade_date, s.first_td),
           last_trade_date  = s.last_td,
           updated_at       = now()
      FROM (SELECT symbol AS cd_symbol, min(trade_date) AS first_td, max(trade_date) AS last_td
            FROM contract_daily GROUP BY symbol) s
     WHERE dc.contract_code = s.cd_symbol;
    RAISE NOTICE '[dim] dim_contract 生命周期已刷新（contract_daily 为源）';
END;
$$;

-- ---------------------------------------------------------------------------
-- sp_build_l2_roll_segment：L1 → L2 换月 offset（append-only）
--   移植 scripts/build_roll_segments.py 双门检测（振幅门 + 指数连比率门 0.40）。
--   输出：roll_segment（symbol, freq, seg_no, seg_start, roll_ts, roll_delta, cum_offset）
--   策略：只 INSERT「比当前最大 seg_start 更新的段」，历史段绝不触碰（append-only）。
--
--   双门（与 Python 版对齐，需云端 A/B）：
--     cc        = close[i] - close[i-1]                  （close-to-close 跳空）
--     amp       = high[i] - low[i];  prev_amp = 上一根 amp
--     is_large  = |cc| > (amp + prev_amp)
--     idx_delta = |index_close[i] - index_close[i-1]|    （8888 指数连）
--     ratio_ok  = idx_delta < 0.40 * |cc|
--     roll      = is_large AND ratio_ok
--   cum_offset(k) = -Σ_{j<=k} roll_delta(j)；段 0（最早）= 0，历史永不改变。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE sp_build_l2_roll_segment(p_freq text DEFAULT 'min15')
LANGUAGE plpgsql AS $$
DECLARE
    v_table   text := l1_table_of(p_freq);
    v_src_n   bigint;
    v_sym     text;
    v_max_seg int;
    v_max_start timestamptz;
    v_sql     text;
BEGIN
    IF v_table IS NULL THEN
        RAISE EXCEPTION 'sp_build_l2_roll_segment: 不支持的 freq=%', p_freq;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename=v_table) THEN
        RAISE EXCEPTION 'sp_build_l2_roll_segment: 源表 % 不存在，拒绝执行', v_table;
    END IF;
    EXECUTE format('SELECT count(*) FROM %I WHERE symbol ~ ''^[A-Za-z]+888$''', v_table) INTO v_src_n;
    IF v_src_n = 0 THEN
        RAISE EXCEPTION 'sp_build_l2_roll_segment: % 主连(888)为 0 行，拒绝执行', v_table;
    END IF;

    FOR v_sym IN
        EXECUTE format('SELECT DISTINCT symbol FROM %I WHERE symbol ~ ''^[A-Za-z]+888$'' ORDER BY symbol', v_table)
    LOOP
        SELECT max(seg_no), max(seg_start) INTO v_max_seg, v_max_start
          FROM roll_segment WHERE symbol = v_sym AND freq = p_freq;
        v_max_seg   := COALESCE(v_max_seg, -1);
        v_max_start := COALESCE(v_max_start, '-infinity');

        v_sql := format($q$
            WITH main AS (
                SELECT bucket, open, high, low, close,
                       lag(close)  OVER w AS prev_close,
                       lag(high)   OVER w - lag(low) OVER w AS prev_amp
                FROM %I WHERE symbol = %L
                WINDOW w AS (ORDER BY bucket)
            ),
            idx AS (
                SELECT bucket, close AS idx_close,
                       lag(close) OVER (ORDER BY bucket) AS idx_prev
                FROM %I WHERE symbol = %L
            ),
            joined AS (
                SELECT m.bucket, m.close, m.prev_close,
                       (m.high - m.low) AS amp, m.prev_amp,
                       (i.idx_close - i.idx_prev) AS idx_delta
                FROM main m LEFT JOIN idx i ON i.bucket = m.bucket
            ),
            events AS (
                SELECT bucket,
                       (close - prev_close) AS cc,
                       (abs(close - prev_close) > (COALESCE(amp,0) + COALESCE(prev_amp,0)))
                           AND (COALESCE(abs(idx_delta), 1e9) < 0.40 * abs(close - prev_close))
                           AS is_roll
                FROM joined WHERE prev_close IS NOT NULL
            ),
            new_rolls AS (
                SELECT bucket AS roll_ts, cc AS roll_delta
                FROM events WHERE is_roll AND bucket > %L::timestamptz
            ),
            numbered AS (
                SELECT roll_ts, roll_delta,
                       row_number() OVER (ORDER BY roll_ts) - 1 AS off
                FROM new_rolls
            )
            INSERT INTO roll_segment
                (symbol, freq, seg_no, seg_start, seg_end, roll_ts, roll_delta, cum_offset,
                 src_freq, updated_at)
            SELECT %L, %L,
                   $1 + 1 + n.off,
                   nr.roll_ts,
                   lead(nr.roll_ts) OVER (ORDER BY nr.roll_ts),
                   nr.roll_ts,
                   nr.roll_delta,
                   -(COALESCE((SELECT sum(roll_delta) FROM roll_segment
                               WHERE symbol=%L AND freq=%L), 0)
                     + sum(nr.roll_delta) OVER (ORDER BY nr.roll_ts)),
                   %L, now()
            FROM numbered n JOIN new_rolls nr USING (roll_ts);
        $q$, v_table, v_sym, v_table, replace(v_sym,'888','8888'), v_max_start,
               v_sym, p_freq, v_sym, p_freq, p_freq);

        EXECUTE v_sql USING v_max_seg;

        EXECUTE format($u$
            UPDATE roll_segment r SET price_shift = (
                SELECT abs(min(cum_offset)) FROM roll_segment WHERE symbol=%L AND freq=%L)
             WHERE r.symbol=%L AND r.freq=%L
            $u$, v_sym, p_freq, v_sym, p_freq);
    END LOOP;
    RAISE NOTICE '[L2] roll_segment(%s) 刷新完成（append-only）', p_freq;
END;
$$;

COMMIT;
