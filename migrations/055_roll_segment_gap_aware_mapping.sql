-- =============================================================================
-- 055 · 换月事件跨休市映射修正（CL-20261107-15 / B）+ 单品种参数化（CL-20261107-16 / C1）
-- 日期：2026-11-08
--
-- 背景 / 决策
-- -----------
-- 2026-09-30 曾拍板「sp 以 15m 锚定原版为准忠实移植，不得简化/改变/添加」（见
-- scripts/cloud_preflight/README.md），009 文件头据此写死 252/252 逐行相等。
-- 2026-11-08 12:05 用户重新拍板：**改算法**。本迁移与
-- scripts/build_roll_segments.py::map_roll_to_freq 同步修改，两版仍须经
-- cloud_preflight/02 A/B 逐行对拍通过后才允许应用。
--
-- 缺陷（两版共有，不是移植偏差）
-- ------------------------------
-- 原逻辑用「事件落在 [bucket, next_bucket) 内」判归属，next_bucket = lead(bucket)。
-- 60m 网格上 15:00 的下一根是 22:00，中间 15:00-21:00 休市没有任何 bar，
-- 于是 15:00 这根的覆盖区间被撑成 7 小时，把 2026-11-04 21:15 的 PF888 真换月
-- 吞进日盘收盘桶。后果：min60 的 15:00 被额外偏移 -240、14:15→15:00 凭空出现
-- +240 假跳空，而 22:00 处真实的 -240 跳空一根没消。
--
-- 修法（最小改动：只在跨休市空隙生效）
-- -----------------------------------
-- 把覆盖上界 clamp 到 min(next_bucket, bucket + 名义周期)，然后补一个分支：
--   ① 事件 ∈ [bucket, bucket+freq)            → 归 bucket（与原逻辑完全一致）
--   ② 事件 ∈ [bucket+freq, next_bucket)       → 归 next_bucket（第一个 >= 事件的桶）
-- 日盘内 next_bucket == bucket+freq，分支②恒空，252 个映射逐行不变；
-- 只有跨休市空隙（next_bucket > bucket+freq）才触发分支②。
-- 末根 bar（next_bucket IS NULL）两分支都不匹配，事件照样丢弃，与原逻辑一致。
--
-- 同时（C1）：加 p_symbol text DEFAULT NULL，循环只处理指定品种，使 B 的验收可以
-- 用 BEGIN; CALL ...('min60', false, 'PF888'); ROLLBACK; 事务内验证，且生产重跑
-- 只需重写 PF888 一个品种，不必 append-only 重算 82 品种、波及刚验收的 B0 基线。
-- ⚠ low_off 的相关子查询与 shift_calc 全表聚合本次**故意不改**（CL-16 的 C2），
--   它是数值计算路径变更；与映射改动同时进入 A/B 对拍会导致失败无法归因。
--   后置到 056。
--
-- 附带修复（不改算法数值）：054 的 INSERT 漏写 n_bars 列，生产 l2_adj.roll_segment
-- 实测 16,972 段全部 n_bars IS NULL（min15 4433 / min30 4242 / min5 4234 / min60 4063）。
-- cloud_preflight/02 的 compare() 把「存在 n_bars IS NULL 段」直接判为不一致，
-- 导致 252/252 验收链在当前代码上永远跑不出 PASS。full_segs 已算出 n_bars，
-- 本迁移把它写进 INSERT，使 A/B 判定重新可信；该列不参与 A/B 比较的 4 个数值列。
--
-- ⚠ 签名从 (text,boolean) 变为 (text,boolean,text)：CREATE OR REPLACE 增参数会造成
--   重载歧义，必须先 DROP。两参调用因 p_symbol 有默认值仍可解析。
-- =============================================================================

BEGIN;

DROP PROCEDURE IF EXISTS sp_build_l2_roll_segment(text, boolean);

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
    SELECT t.bucket AS bar_ts, e.gap
    FROM target t
    JOIN roll_events e
      ON e.a_ts >= t.bucket
     AND e.a_ts < least(t.bucket + %3$L::interval,
                        coalesce(t.next_bucket, t.bucket + %3$L::interval))
    UNION ALL
    SELECT t.next_bucket AS bar_ts, e.gap
    FROM target t
    JOIN roll_events e
      ON e.a_ts >= t.bucket + %3$L::interval
     AND e.a_ts < t.next_bucket
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
    'L1→L2 换月 offset（append-only）。055 / CL-20261107-15：mapped 上界 clamp 到 '
    'min(next_bucket, bucket+名义周期)，跨休市空隙内的事件改归第一根 >= 事件的桶；'
    '同时加 p_symbol 支持单品种重跑。low_off 仍未优化（CL-16 C2，后置 056）。';

COMMIT;
