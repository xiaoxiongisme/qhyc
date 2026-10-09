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
--   sp_build_l1_from_minute(p_freq)       L0(1m) → L1(Xm) 经典 time_bucket 聚合（当前 1m 缺失→安全 no-op）
--   sp_normalize_l1_symbols()            L1 符号审计：报告 bar_* 中无法经 dim_symbol 解析为 888/8888 的符号
--   sp_refresh_dim_contract_dates()      由 contract_daily 回填 dim_contract 生命周期（只增改）
--   sp_build_l2_roll_segment(p_freq[, p_positivity])
--        L1 → L2 换月 offset（append-only，**忠实移植** scripts/build_roll_segments.py）。
--        与原版逐项对齐；任何偏离都视为回归，须经云端 02 预检 A/B 逐行相等复核。
--        硬约束（来自 PRD + 用户踩坑 + 252/252 验收，不得简化/改变/添加）：
--          ★ 换月检测固定 15m 锚点（ANCHOR_FREQ='min15'），再映射到目标 freq（含关系映射）
--          ★ delta 取锚点 gap = open[i]-close[i-1]（非 cc，否则各周期偏移发散，实测差 1560 点）
--          ★ 双门：is_large = |cc| > (amp+1e-6)；ratio = |Δ8888| < 0.40|cc|；roll = 二者皆真
--          ★ cum_offset(k) = -Σ_{j<=k} delta(j)；段 0 偏移恒 0，历史段永不重算
--          ★ price_shift 默认 0（与原版默认不带 --positivity 一致）；仅 p_positivity=true 时
--            取 ceil((-min+0.01·range)/100)·100（仅当 min(low+cum)<=0）
--          ★ src_freq 恒 'min15'
--
-- ⚠ 验证要求（无本地 DB 可连，本迁移只保证 DDL 合法，逻辑需在云端 dry-run）
--   sp_build_l2_roll_segment 与 scripts/build_roll_segments.py（252/252 验收）为同一算法，
--   云端预检 02_fixture_and_roll_ab.py 应得出**逐行相等**的 A/B 结论（默认 positivity 关闭时）。
--   唯一预期差异：sp 的 p_positivity 开关（默认关，与原版默认一致）。
--   L2 为 append-only，错跑只产生需复核的段，可
--     DELETE FROM roll_segment WHERE symbol=? AND freq=? AND seg_no>已知正确末段 安全回退。
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
-- 2026-10-08 二次改造：procedure 自身按时间分块 + 增量聚合，从根上避开 TimescaleDB
-- 解压上限（默认 100k）与 OOM。调度器日常只需 CALL sp_build_l1_from_minute('min5')
-- （不传 p_from）→ 增量模式：只聚"目标表最大 bucket 之后"的新数据；历史回填传
-- p_from/p_to 指定区间，procedure 内部按 1 天/块循环（每块 ≤~54k 行 < 100k 上限，
-- 无需改 GUC、不会 OOM）。CREATE OR REPLACE 不能改参数量，先 DROP 旧签名再建新签名。
DROP PROCEDURE IF EXISTS sp_build_l1_from_minute(text);
DROP PROCEDURE IF EXISTS sp_build_l1_from_minute(text, timestamptz, timestamptz);

CREATE OR REPLACE PROCEDURE sp_build_l1_from_minute(
    p_freq text DEFAULT 'min15',
    p_from timestamptz DEFAULT NULL,
    p_to   timestamptz DEFAULT NULL)
LANGUAGE plpgsql AS $$
DECLARE
    v_table     text := l1_table_of(p_freq);
    v_src       text := 'minute_bar';
    v_dst_n     bigint;
    v_interval  interval := CASE p_freq
        WHEN 'min5'  THEN interval '5 minutes'
        WHEN 'min15' THEN interval '15 minutes'
        WHEN 'min30' THEN interval '30 minutes'
        WHEN 'min60' THEN interval '60 minutes'
        ELSE NULL END;
    v_from      timestamptz;
    v_to        timestamptz;
    v_cur       timestamptz;
    v_end       timestamptz;
    v_wm        timestamptz;
    v_chunk     interval := interval '1 day';   -- 每块 ≤~54k 行，远低于 100k 解压上限
    v_n_chunk   int := 0;
BEGIN
    IF v_table IS NULL OR v_interval IS NULL THEN
        RAISE EXCEPTION 'sp_build_l1_from_minute: 不支持的 freq=%（仅 min5/15/30/60）', p_freq;
    END IF;
    -- public.minute_bar 是视图（底层 l0_raw.minute_bar）；pg_tables 仅登记实表会漏掉它。
    -- 改用 to_regclass 兼识表与视图，避免误判"源表不存在"而 safe no-op。
    IF to_regclass('public.' || v_src) IS NULL THEN
        RAISE NOTICE '[L1] 源表 % 不存在，跳过', v_src; RETURN;
    END IF;

    -- 范围确定
    IF p_from IS NOT NULL THEN
        -- 回填模式：处理显式 [p_from, p_to) 区间（p_to 缺省则到最新整桶）
        v_from := p_from;
        IF p_to IS NOT NULL THEN
            v_to := p_to;
        ELSE
            SELECT time_bucket(v_interval, max(ts)) INTO v_to FROM minute_bar;
        END IF;
    ELSE
        -- 增量模式：从目标表最大 bucket 的下一桶开始，到分钟表最新整桶为止（只聚新数据）
        EXECUTE format('SELECT max(bucket) FROM %I', v_table) INTO v_wm;
        IF v_wm IS NULL THEN
            EXECUTE format('SELECT min(time_bucket($1, ts)) FROM %I WHERE symbol ~ ''^[A-Za-z]+8888?$''', v_src)
                USING v_interval INTO v_from;
        ELSE
            v_from := v_wm + v_interval;
        END IF;
        SELECT time_bucket(v_interval, max(ts)) INTO v_to FROM minute_bar;
    END IF;

    IF v_from IS NULL OR v_to IS NULL OR v_from >= v_to THEN
        RAISE NOTICE '[L1] % 无新数据可聚合（from=% / to=%），跳过', v_table, v_from, v_to;
        RETURN;
    END IF;

    -- 按 1 天/块循环聚合；每块独立 INSERT…SELECT，单次解压 ≤~54k 行，远低于 100k 上限，不 OOM
    v_cur := v_from;
    LOOP
        EXIT WHEN v_cur >= v_to;
        v_end := least(v_cur + v_chunk, v_to);
        EXECUTE format($q$
            INSERT INTO %I (symbol, bucket, open, high, low, close, volume, open_interest)
            SELECT symbol,
                   time_bucket($1, ts) AS bucket,
                   first(open, ts)  AS open,
                   max(high)        AS high,
                   min(low)         AS low,
                   last(close, ts)  AS close,
                   sum(volume)             AS volume,
                   max(open_interest)      AS open_interest
            FROM minute_bar
            WHERE symbol ~ '^[A-Za-z]+8888?$' AND ts >= $2 AND ts < $3
            GROUP BY symbol, time_bucket($1, ts)
            ON CONFLICT (symbol, bucket) DO UPDATE
               SET open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                   close=EXCLUDED.close, volume=EXCLUDED.volume, open_interest=EXCLUDED.open_interest
            $q$, v_table)
            USING v_interval, v_cur, v_end;
        v_n_chunk := v_n_chunk + 1;
        v_cur := v_end;
    END LOOP;

    EXECUTE format('SELECT count(*) FROM %I', v_table) INTO v_dst_n;
    RAISE NOTICE '[L1] % 增量/分块聚合完成：% 天块，目标表现有 % 行', v_table, v_n_chunk, v_dst_n;
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
        IF to_regclass('public.' || r.t) IS NULL THEN
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
    IF to_regclass('public.contract_daily') IS NULL THEN
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
-- sp_build_l2_roll_segment：L1 → L2 换月 offset（append-only，**忠实移植**）
--   与 scripts/build_roll_segments.py（252/252 验收）逐项对齐，硬约束见文件头。
--   落地：首跑（该 sym/freq 无段）全量插入；增量只插 seg_start > 已有最大 seg_start
--        的段，cum_offset 接龙（= 末段 cum_offset − 新累计 roll_delta）。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE sp_build_l2_roll_segment(p_freq text DEFAULT 'min15',
                                                    p_positivity boolean DEFAULT false)
LANGUAGE plpgsql AS $$
DECLARE
    v_table     text := l1_table_of(p_freq);
    v_anchor    text := 'bar_15m';                 -- 固定锚点，与 Python ANCHOR_FREQ 一致
    v_fallback  text := CASE p_freq WHEN 'min5' THEN '5 minutes' WHEN 'min15' THEN '15 minutes'
                                     WHEN 'min30' THEN '30 minutes' WHEN 'min60' THEN '60 minutes' END;
    v_sym       text;
    v_anchor_n  bigint;
    v_idx_n     bigint;
    v_max_seg   int;
    v_max_start timestamptz;
    v_base_co   numeric;
    v_cte       text;
    v_ins_n     int;
BEGIN
    IF v_table IS NULL OR v_fallback IS NULL THEN
        RAISE EXCEPTION 'sp_build_l2_roll_segment: 不支持的 freq=%（仅 min5/15/30/60）', p_freq;
    END IF;
    -- 15m 锚点是硬依赖：缺失则无法锚定，拒绝执行（fail-loud）
    IF to_regclass('public.' || v_anchor) IS NULL THEN
        RAISE EXCEPTION 'sp_build_l2_roll_segment: 锚点表 % 不存在，15m 锚定无法进行，拒绝执行', v_anchor;
    END IF;
    IF to_regclass('public.' || v_table) IS NULL THEN
        RAISE EXCEPTION 'sp_build_l2_roll_segment: 目标表 % 不存在，拒绝执行', v_table;
    END IF;

    FOR v_sym IN
        EXECUTE format('SELECT DISTINCT symbol FROM %I WHERE symbol ~ ''^[A-Za-z]+888$'' ORDER BY symbol', v_table)
    LOOP
        -- 锚点品种自身需足够 15m 数据；不足则跳过（与原版 len<100 skip 一致）
        EXECUTE format('SELECT count(*) FROM %I WHERE symbol=%L', v_anchor, v_sym) INTO v_anchor_n;
        IF v_anchor_n < 100 THEN
            RAISE NOTICE '[L2] % 15m 锚点不足 100 行，跳过', v_sym; CONTINUE;
        END IF;
        -- 8888 指数连须存在，否则无法判 ratio 门（原版：无 8888 整体 skip）
        EXECUTE format('SELECT count(*) FROM %I WHERE symbol=%L', v_anchor, replace(v_sym,'888','8888')) INTO v_idx_n;
        IF v_idx_n = 0 THEN
            RAISE NOTICE '[L2] % 无 8888 指数连，跳过', v_sym; CONTINUE;
        END IF;

        -- 共享 CTE：15m 锚定检测 → 映射到目标 bar → 切段 → cum_offset → 可选 positivity
        -- 用美元引号，内部单引号无需转义；动态值经 format(%1$L/%2$I/%3$L) 注入
        -- ⚠ 必须置于循环内 v_sym 已赋值之后（CTE 引用 %1$L=v_sym）
        v_cte := format($c$
WITH anchor AS (
    SELECT m.bucket AS a_ts,
           (m.open - lag(m.close) OVER (ORDER BY m.bucket)) AS gap,
           (m.close - lag(m.close) OVER (ORDER BY m.bucket)) AS cc,
           ((m.high - m.low) + coalesce(lag(m.high - m.low) OVER (ORDER BY m.bucket), 0)) AS amp,
           abs(i.close - lag(i.close) OVER (ORDER BY i.bucket)) AS idx_delta
    FROM bar_15m m
    LEFT JOIN bar_15m i ON i.bucket = m.bucket AND i.symbol = replace(m.symbol, '888', '8888')
    WHERE m.symbol = %1$L
),
roll_events AS (
    SELECT a_ts, gap FROM anchor
    WHERE cc IS NOT NULL
      AND abs(cc) > (amp + 1e-6)
      AND coalesce(idx_delta, 1e9) < 0.40 * abs(cc)
),
target AS (
    SELECT bucket, lead(bucket) OVER (ORDER BY bucket) AS next_bucket
    FROM %2$I WHERE symbol = %1$L
),
mapped AS (
    SELECT t.bucket AS bar_ts, e.gap
    FROM target t
    JOIN roll_events e
      ON e.a_ts >= t.bucket
     AND e.a_ts < coalesce(t.next_bucket, t.bucket + %3$L::interval)
),
roll_bars AS ( SELECT bar_ts, sum(gap) AS delta_ev FROM mapped GROUP BY bar_ts ),
all_bars AS (
    SELECT bucket, row_number() OVER (ORDER BY bucket) - 1 AS rn
    FROM %2$I WHERE symbol = %1$L
),
flags AS (
    SELECT a.bucket,
           CASE WHEN rb.delta_ev IS NOT NULL THEN true ELSE false END AS is_roll,
           coalesce(rb.delta_ev, 0) AS delta_ev
    FROM all_bars a LEFT JOIN roll_bars rb ON rb.bar_ts = a.bucket
),
segs AS (
    SELECT bucket, delta_ev, is_roll,
           count(*) FILTER (WHERE is_roll) OVER (ORDER BY bucket) AS seg_no
    FROM flags
),
seg_final AS (
    SELECT seg_no, min(bucket) AS seg_start, lead(min(bucket)) OVER (ORDER BY seg_no) AS seg_end,
           (array_agg(delta_ev ORDER BY bucket))[1] AS first_delta, count(*) AS n_bars
    FROM segs GROUP BY seg_no
),
full_segs AS (
    SELECT seg_no, seg_start, seg_end, n_bars,
           CASE WHEN seg_no = 0 THEN 0::numeric ELSE round(first_delta,4) END AS roll_delta,
           -sum(CASE WHEN seg_no=0 THEN 0::numeric ELSE round(first_delta,4) END)
               OVER (ORDER BY seg_no) AS cum_offset
    FROM seg_final
),
low_off AS (
    SELECT l.low + (
        SELECT s.cum_offset FROM full_segs s
        WHERE s.seg_start <= l.bucket AND (s.seg_end IS NULL OR l.bucket < s.seg_end)
        ORDER BY s.seg_start DESC LIMIT 1) AS v
    FROM %2$I l WHERE l.symbol = %1$L
),
shift_calc AS (
    SELECT CASE WHEN min(v) <= 0 THEN
        ceil((-min(v) + 0.01 * greatest((SELECT max(low)-min(low) FROM %2$I WHERE symbol=%1$L), 1.0)) / 100.0) * 100.0
    ELSE 0 END AS shift FROM low_off
)
$c$, v_sym, v_table, v_fallback);

        -- 既有段（append-only 起点）
        SELECT max(seg_no), max(seg_start) INTO v_max_seg, v_max_start
          FROM roll_segment WHERE symbol = v_sym AND freq = p_freq;
        v_max_seg   := COALESCE(v_max_seg, -1);
        v_max_start := COALESCE(v_max_start, '-infinity');

        IF v_max_seg < 0 THEN
            -- 首跑：全量插入（seg_no 沿用 full_segs 计算值）
            EXECUTE format(
                'INSERT INTO roll_segment (symbol, freq, seg_no, seg_start, seg_end, roll_ts, '
                'roll_delta, cum_offset, price_shift, src_freq, updated_at) '
                || v_cte ||
                ' SELECT %1$L, %2$L, fs.seg_no, fs.seg_start, fs.seg_end, fs.seg_start, '
                'fs.roll_delta, fs.cum_offset, CASE WHEN %3$L THEN sc.shift ELSE 0 END, '
                '''min15'', now() FROM full_segs fs CROSS JOIN shift_calc sc',
                v_sym, p_freq, p_positivity);
            GET DIAGNOSTICS v_ins_n = ROW_COUNT;
            RAISE NOTICE '[L2] %/% 首跑插入 % 段（忠实移植·15m 锚定）', v_sym, p_freq, v_ins_n;
        ELSE
            -- 增量：只插新段，cum_offset 接龙（= 末段 cum − 新累计 roll_delta）
            SELECT cum_offset INTO v_base_co
              FROM roll_segment WHERE symbol = v_sym AND freq = p_freq AND seg_no = v_max_seg;
            v_base_co := COALESCE(v_base_co, 0);
            EXECUTE format(
                'INSERT INTO roll_segment (symbol, freq, seg_no, seg_start, seg_end, roll_ts, '
                'roll_delta, cum_offset, price_shift, src_freq, updated_at) '
                || v_cte ||
                ', new_segs AS (SELECT *, row_number() OVER (ORDER BY seg_start) - 1 AS new_off '
                'FROM full_segs WHERE seg_start > %1$L::timestamptz) '
                'SELECT %2$L, %3$L, %4$L + 1 + ns.new_off, ns.seg_start, ns.seg_end, ns.seg_start, '
                'ns.roll_delta, (%5$L) - sum(ns.roll_delta) OVER (ORDER BY ns.seg_start), '
                'CASE WHEN %6$L THEN sc.shift ELSE 0 END, ''min15'', now() '
                'FROM new_segs ns CROSS JOIN shift_calc sc',
                v_max_start, v_sym, p_freq, v_max_seg, v_base_co, p_positivity);
            GET DIAGNOSTICS v_ins_n = ROW_COUNT;
            RAISE NOTICE '[L2] %/% 增量插入 % 段（接龙末段 seg_no=%）', v_sym, p_freq, v_ins_n, v_max_seg;
        END IF;
    END LOOP;
    RAISE NOTICE '[L2] roll_segment(%s) 刷新完成（append-only，忠实移植 15m 锚定）', p_freq;
END;
$$;

COMMIT;
