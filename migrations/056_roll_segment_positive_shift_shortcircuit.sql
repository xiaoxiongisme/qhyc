-- =============================================================================
-- 056 · 后复权 shift 计算短路 + low_off 改 JOIN（CL-20261107-16 / C2）
-- 日期：2026-11-08
-- 依赖：055（三参签名 + 跨休市映射修正）。本迁移只替换过程体，**不改签名、
--       不改映射逻辑、不改 A/B 比较的 4 个数值列**。
--
-- 问题（CL-20261107-16 / C2）
-- --------------------------
-- 055 之前 `low_off` 对**每根 bar** 跑一次相关子查询（ORDER BY seg_start DESC
-- LIMIT 1），`shift_calc` 又对该品种做一次 `max(low)-min(low)` 全表聚合。
-- min5 实测：l1_mkt.bar_5m = 3477 MB / 268 chunks / 82 品种，PF888 单个品种
-- 就有 106,453 根；PG 容器内存上限仅 3 GiB（docker inspect Memory=3221225472），
-- work_mem=64MB。全量 min5 重跑两次把 PG 打到 OOM（42 / 71 分钟），并让
-- ALTER TABLE fusion_position 堵锁 58 分钟。min5 段自 2026-11-06 12:48 起断更。
--
-- 关键事实：`shift_calc` 的结果**只在 p_positivity=true 时被使用**
-- （INSERT 里 `CASE WHEN p_positivity THEN sc.shift ELSE 0 END`），而生产所有
-- 调用方都走默认 false。也就是说 OOM 的那两坨计算，结果恒被丢弃。
--
-- 修法（两条，都不改映射、不改段数值）
-- -----------------------------------
-- 1) `WHERE l.symbol = ... AND %4$L::boolean`（%4 = p_positivity，format 输出 'f'/'t'）。
--    false 时 PostgreSQL 把 'f'::boolean 常数折叠，直接判空集，`low_off` 零扫描
--    → 默认路径彻底短路。⚠ 不能用 `%s`：PG 的 boolean text 输出是单字母 `f`/`t`，
--    `AND f` 会被解析成列名并报 UndefinedColumn（2026-11-08 staging 实测）。
--    `shift_calc` 在空集上聚合返回一行，coalesce(...,0) 保证 CROSS JOIN 仍有 0。
-- 2) p_positivity=true 时：相关子查询改 LEFT JOIN full_segs 的区间连接
--    （段不重叠，最多匹配 1 行；无匹配时 v 仍为 NULL，与原逻辑一致），
--    `max(low)-min(low)` 改为从 low_off 自己取，消除第二遍全表扫描。
--
-- 与 055 的关系：按 2026-11-08 12:05 拍板顺序，C2 必须等 B 的 A/B 对拍通过
-- 才实施（避免数值路径变更与映射变更混在一起、失败无法归因）。055 的 B 已验收，
-- 故本迁移单独替换过程体。签名仍为 (text,boolean,text)，用 CREATE OR REPLACE，
-- 不 DROP、不产生重载歧义。
-- =============================================================================

BEGIN;

-- 签名不变，直接替换过程体；不 DROP（避免 054 两参重载歧义）。
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
    SELECT l.low + s.cum_offset AS v, l.low AS low
    FROM %2$I l
    LEFT JOIN full_segs s
      ON l.bucket >= s.seg_start
     AND (s.seg_end IS NULL OR l.bucket < s.seg_end)
    WHERE l.symbol = %1$L
      AND %4$L::boolean         -- false → 'f'::boolean 常量折叠 → 空集，零扫描
),
shift_calc AS (
    SELECT coalesce(CASE WHEN min(v) <= 0 THEN
        ceil((-min(v) + 0.01 * greatest(max(low) - min(low), 1.0)) / 100.0) * 100.0
    ELSE 0 END, 0) AS shift FROM low_off
)
$c$, v_sym, v_table, v_fallback, p_positivity);

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
            RAISE NOTICE '[L2] %/% 首跑插入 % 段（056 shift 短路版）', v_sym, p_freq, v_ins_n;
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

    RAISE NOTICE '[L2] roll_segment(%) 刷新完成 symbol=%（056 shift 短路版）', p_freq, coalesce(p_symbol, 'ALL');
END;
$$;

COMMENT ON PROCEDURE sp_build_l2_roll_segment(text, boolean, text) IS
    'L1→L2 换月 offset（append-only）。055：跨休市映射修正 + p_symbol + 补 n_bars；'
    '056 / CL-20261107-16 C2：p_positivity=false 时 low_off 常数折叠短路（默认路径'
    '零全表扫描），true 时相关子查询改区间 JOIN 且 shift_calc 不再二次全表聚合，'
    '消除 min5 两次 OOM。映射与段数值一律不变。';

COMMIT;
