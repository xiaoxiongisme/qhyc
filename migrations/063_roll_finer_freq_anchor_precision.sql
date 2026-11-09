-- =============================================================================
-- CL-20261108-06：15m 锚点映射到**更细周期**（min5）时的右标签精度修正
--
-- 症状：PF888 2026-11-04 夜盘 21:00 换月，bar_5m_ba 的 21:05/21:10 两根仍是
--       旧 offset(80)，21:15 才跳到 320 → 连续序列上凭空多出 240 点假跳空
--       （真实连续价应为 8360/8368，实际是 8120/8128）。21:15 之后正确。
--
-- 根因：15m bucket 是右标签（桶 a 覆盖 [a-15m, a)），锚点 gap 定义为
--       `open - 前一根 close`，即跳空发生在桶的**开盘第一笔** = 桶内**第一根**
--       细周期 bar。旧规则「最后一个 ts <= a」取的是桶**末**根，把跳空推到桶尾。
--
-- 修法：freq < 锚点(15m) 时改取桶内第一根（分支①）；桶内无 bar（早年 5m
--       数据稀疏）时回退原规则（分支②/③），保证换月事件一个都不丢。
--       非 min5 频率的 SQL 文本与执行结果逐字不变。
--
-- 配套：scripts/build_roll_segments.py::map_roll_to_freq 同步（双真源对齐）；
--       段表变更后由 sp_build_l2_back_adjusted 的触发签名（Σepoch(seg_start)/段数）
--       自动判 full 并级联重建 bar_5m_ba。
-- =============================================================================

BEGIN;

CREATE OR REPLACE PROCEDURE sp_build_l2_roll_segment(p_freq text DEFAULT 'min15',
                                                    p_positivity boolean DEFAULT false,
                                                    p_symbol text DEFAULT NULL)
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
    IF to_regclass('public.' || v_anchor) IS NULL THEN
        RAISE EXCEPTION 'sp_build_l2_roll_segment: 锚点表 % 不存在，15m 锚定无法进行，拒绝执行', v_anchor;
    END IF;
    IF to_regclass('public.' || v_table) IS NULL THEN
        RAISE EXCEPTION 'sp_build_l2_roll_segment: 目标表 % 不存在，拒绝执行', v_table;
    END IF;

    FOR v_sym IN
        EXECUTE format('SELECT DISTINCT symbol FROM %1$I '
                       'WHERE symbol ~ ''^[A-Za-z]+888$'' '
                       'AND (%2$L IS NULL OR symbol = %2$L) ORDER BY symbol',
                       v_table, p_symbol)
    LOOP
        EXECUTE format('SELECT count(*) FROM %I WHERE symbol=%L', v_anchor, v_sym) INTO v_anchor_n;
        IF v_anchor_n < 100 THEN
            RAISE NOTICE '[L2] % 15m 锚点不足 100 行，跳过', v_sym; CONTINUE;
        END IF;
        EXECUTE format('SELECT count(*) FROM %I WHERE symbol=%L', v_anchor, replace(v_sym,'888','8888')) INTO v_idx_n;
        IF v_idx_n = 0 THEN
            RAISE NOTICE '[L2] % 无 8888 指数连，跳过', v_sym; CONTINUE;
        END IF;

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
    -- ① CL-20261108-06：比锚点更细的周期（min5）取「锚点桶内第一根」。
    --    15m bucket 是右标签，桶 a 覆盖 [a-15m, a)；锚点 gap = open - 前一根 close，
    --    即跳空落在桶的开盘第一笔 = 桶内第一根细周期 bar。旧规则取桶**末**根，
    --    把夜盘 21:00 换月推到 21:15（PF888 2026-11-04 实测）。
    SELECT fb.bucket AS bar_ts, e.gap
    FROM roll_events e
    JOIN LATERAL (
        SELECT f2.bucket FROM %2$I f2
         WHERE f2.symbol = %1$L
           AND f2.bucket >  e.a_ts - interval '15 minutes'
           AND f2.bucket <= e.a_ts
         ORDER BY f2.bucket
         LIMIT 1
    ) fb ON true
    WHERE %4$L = 'min5'
    UNION ALL
    -- ② 原规则：包含关系，上界 clamp 到名义周期（CL-15）。细周期仅在桶内无 bar 时兜底
    SELECT t.bucket AS bar_ts, e.gap
    FROM target t
    JOIN roll_events e
      ON e.a_ts >= t.bucket
     AND e.a_ts < least(t.bucket + %3$L::interval,
                        coalesce(t.next_bucket, t.bucket + %3$L::interval))
     AND (%4$L <> 'min5' OR NOT EXISTS (
            SELECT 1 FROM %2$I f3
             WHERE f3.symbol = %1$L
               AND f3.bucket >  e.a_ts - interval '15 minutes'
               AND f3.bucket <= e.a_ts))
    UNION ALL
    -- ③ 跨休市空隙内的事件归下一根（原样保留 CL-15）
    SELECT t.next_bucket AS bar_ts, e.gap
    FROM target t
    JOIN roll_events e
      ON e.a_ts >= t.bucket + %3$L::interval
     AND e.a_ts < t.next_bucket
     AND (%4$L <> 'min5' OR NOT EXISTS (
            SELECT 1 FROM %2$I f3
             WHERE f3.symbol = %1$L
               AND f3.bucket >  e.a_ts - interval '15 minutes'
               AND f3.bucket <= e.a_ts))
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
$c$, v_sym, v_table, v_fallback, p_freq);

        SELECT max(seg_no), max(seg_start) INTO v_max_seg, v_max_start
          FROM roll_segment WHERE symbol = v_sym AND freq = p_freq;
        v_max_seg   := COALESCE(v_max_seg, -1);
        v_max_start := COALESCE(v_max_start, '-infinity');

        IF v_max_seg < 0 THEN
            EXECUTE format(
                'INSERT INTO roll_segment (symbol, freq, seg_no, seg_start, seg_end, roll_ts, '
                'roll_delta, cum_offset, n_bars, price_shift, src_freq, updated_at) '
                || v_cte ||
                ' SELECT %1$L, %2$L, fs.seg_no, fs.seg_start, fs.seg_end, fs.seg_start, '
                'fs.roll_delta, fs.cum_offset, fs.n_bars, CASE WHEN %3$L THEN sc.shift ELSE 0 END, '
                '''min15'', now() FROM full_segs fs CROSS JOIN shift_calc sc',
                v_sym, p_freq, p_positivity);
            GET DIAGNOSTICS v_ins_n = ROW_COUNT;
            RAISE NOTICE '[L2] %/% 首跑插入 % 段（跨休市映射修正版）', v_sym, p_freq, v_ins_n;
        ELSE
            SELECT cum_offset INTO v_base_co
              FROM roll_segment WHERE symbol = v_sym AND freq = p_freq AND seg_no = v_max_seg;
            v_base_co := COALESCE(v_base_co, 0);
            EXECUTE format(
                'INSERT INTO roll_segment (symbol, freq, seg_no, seg_start, seg_end, roll_ts, '
                'roll_delta, cum_offset, n_bars, price_shift, src_freq, updated_at) '
                || v_cte ||
                ', new_segs AS (SELECT *, row_number() OVER (ORDER BY seg_start) - 1 AS new_off '
                'FROM full_segs WHERE seg_start > %1$L::timestamptz) '
                'SELECT %2$L, %3$L, %4$L + 1 + ns.new_off, ns.seg_start, ns.seg_end, ns.seg_start, '
                'ns.roll_delta, (%5$L) - sum(ns.roll_delta) OVER (ORDER BY ns.seg_start), '
                'ns.n_bars, CASE WHEN %6$L THEN sc.shift ELSE 0 END, ''min15'', now() '
                'FROM new_segs ns CROSS JOIN shift_calc sc',
                v_max_start, v_sym, p_freq, v_max_seg, v_base_co, p_positivity);
            GET DIAGNOSTICS v_ins_n = ROW_COUNT;
            RAISE NOTICE '[L2] %/% 增量插入 % 段（接龙末段 seg_no=%）', v_sym, p_freq, v_ins_n, v_max_seg;
        END IF;
    END LOOP;

    -- CL-20261107-14 的 seg_end 接龙收口；C1 收窄到 p_symbol，避免单品种调用扫全表
    UPDATE roll_segment r
       SET seg_end = nx.next_start,
           updated_at = now()
      FROM (
          SELECT symbol, freq, seg_no,
                 lead(seg_start) OVER (PARTITION BY symbol, freq ORDER BY seg_start) AS next_start
          FROM roll_segment
          WHERE freq = p_freq
            AND (p_symbol IS NULL OR symbol = p_symbol)
      ) nx
     WHERE r.symbol = nx.symbol
       AND r.freq   = nx.freq
       AND r.seg_no = nx.seg_no
       AND nx.next_start IS NOT NULL
       AND (r.seg_end IS NULL OR r.seg_end <> nx.next_start);
    GET DIAGNOSTICS v_fix_n = ROW_COUNT;
    IF v_fix_n > 0 THEN
        RAISE NOTICE '[L2] roll_segment(%) seg_end 收口 % 段', p_freq, v_fix_n;
    END IF;

    RAISE NOTICE '[L2] roll_segment(%) 刷新完成 symbol=%（跨休市映射修正版）', p_freq, coalesce(p_symbol, 'ALL');
END;
$$;


COMMENT ON PROCEDURE sp_build_l2_roll_segment(text, boolean, text) IS
    'L1→L2 换月 offset（append-only）。055/CL-20261107-15：mapped 上界 clamp 到 '
    'min(next_bucket, bucket+名义周期)。063/CL-20261108-06：freq<锚点(15m) 时取'
    '锚点桶内第一根（右标签修正），桶内无 bar 回退原规则。';

COMMIT;
