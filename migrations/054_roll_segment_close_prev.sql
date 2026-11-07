-- =============================================================================
-- 054 · 修复 sp_build_l2_roll_segment 的 seg_end 断链（append-only 漏回填）
-- 日期：2026-11-07　台账：CL-20261107-14
--
-- 现象
-- ----
-- min60 有 8 个品种同时存在 **2 个** `seg_end IS NULL` 的段（正常应恰好 1 个，
-- 即末段）。其余三个频率当时恰好各有 1 个，是因为它们自上次全量后还没有追加过
-- 新段；只要追加一次就会复现。
--
-- 根因
-- ----
-- 本过程是纯 append：
--   * 首跑分支按 `lead(min(bucket))` 算好整条链的 seg_end 再整体插入，所以当时是对的；
--   * 增量分支只插「seg_start > 已有最大 seg_start」的新段，而**上一段的 seg_end
--     永远停在 NULL**——没有任何语句回头补它。
-- 于是每追加一次，open 段就多一个。下游按段区间取 offset
-- （`seg_start <= t AND (seg_end IS NULL OR t < seg_end)`）时，同一段时间会同时
-- 命中多段，取哪一段取决于 `ORDER BY seg_start DESC LIMIT 1` 这种巧合。
--
-- 修法
-- ----
-- 在 `END LOOP` 之后按 `p_freq` 统一收口一次（首跑/增量两条路径都覆盖）：
-- 用 `lead(seg_start) PARTITION BY symbol ORDER BY seg_start` 接龙，
-- 保证**只有最后一段**是开区间。幂等，可反复执行。
--
-- 为什么放在循环外而不是循环内
-- ----------------------------
-- 循环只遍历「bar 表里还有数据的符号」；放在循环内会漏掉「bar 数据已停更但
-- roll_segment 仍有历史段」的符号，而那些正是最需要收口的。
-- =============================================================================

BEGIN;

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
    v_fix_n     int;
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

    -- ★ CL-20261107-14：seg_end 断链收口（放在循环外，见文件头「为什么放在循环外」）
    --   不变量：每个 (symbol, freq) 恰好只有最后一段的 seg_end 为 NULL。
    UPDATE roll_segment r
       SET seg_end = nx.next_start,
           updated_at = now()
      FROM (
          SELECT symbol, freq, seg_no,
                 lead(seg_start) OVER (PARTITION BY symbol, freq ORDER BY seg_start) AS next_start
          FROM roll_segment
          WHERE freq = p_freq
      ) nx
     WHERE r.symbol = nx.symbol
       AND r.freq   = nx.freq
       AND r.seg_no = nx.seg_no
       AND nx.next_start IS NOT NULL
       AND (r.seg_end IS NULL OR r.seg_end <> nx.next_start);
    GET DIAGNOSTICS v_fix_n = ROW_COUNT;
    IF v_fix_n > 0 THEN
        RAISE NOTICE '[L2] roll_segment(%) seg_end 收口 % 段（append-only 补链）', p_freq, v_fix_n;
    END IF;

    RAISE NOTICE '[L2] roll_segment(%s) 刷新完成（append-only，忠实移植 15m 锚定）', p_freq;
END;
$$;

COMMENT ON PROCEDURE sp_build_l2_roll_segment(text, boolean) IS
    'L1→L2 换月 offset（append-only）。2026-11-07 CL-20261107-14 增补 seg_end 接龙收口：'
    '此前增量追加新段时从不回填上一段的 seg_end，导致同一 symbol 出现多个 open 段。';

COMMIT;
