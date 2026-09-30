-- =============================================================================
-- 010 · 修复 sp_build_l2_roll_segment 的浮点口径（15m 双门判定必须与 Python 同为 float64）
-- 日期：2026-10-01　发现源：Step3 全市场重生成后与旧快照差 30 段
-- 适用：本地库与云端库同构执行（幂等，CREATE OR REPLACE）
--
-- ---------------------------------------------------------------------------
-- 【问题现象】（2026-10-01 实测，证据链完整）
--   009 的 sp 全市场重生成后 roll_segment = 11496 行，旧快照 roll_segment_bak_20261001
--   = 11526 行，缺 30 段。三方对拍裁决（ scripts/_arbiter_roll.py ）：
--       Python 基准 == 旧备份，sp 单方面少段  ⇒ 判定为 sp BUG，不是数据变化。
--   缺失集中在 5 个冷门品种，全部落在「交易小节第一根 bar」：
--       TS888 少 5（2019-05/06/08 的 11:30、13:00，国债 1 tick = 0.005）
--       FB888 少 2、BB888 少 1、PM888 少 1、WH888 少 1（多为 09:00 开盘首根）
--   min5 完全相等（2876=2876）⇒ 排除「15m 锚点门控公式写错」（否则 5m 也会少），
--   缩小到「零振幅 bar + 指数连差值恰好等于阈值」这一边界。
--
-- 【根因】numeric 精确算术 vs float64 运算
--   TS888 · 2019-05-30 13:00 这一点的实测中间量（17 位小数）：
--       cc          SQL= 0.005                  PY= 0.0049999999999954525
--       idx_delta   SQL= 0.002                  PY= 0.001999999999995339
--       B门余量     SQL= 0.0   （精确相等）       PY= -2.842e-15
--       roll        SQL= False                  PY= True
--   数学上 |Δ8888| = 0.002 与 0.40×|cc| = 0.40×0.005 = 0.002 **完全相等**，
--   严格小于号两边都应是 False；Python 判 True 纯粹是 float64 累积舍入让它低了 1e-15。
--
--   而 SQL 侧之所以"过于精确"，是因为 009 的写法是**先做 numeric 精确减法、最后才 cast**：
--       (i.close - lag(i.close) OVER (...))::double precision      ← cast 在最外层
--   numeric 的 0.002 − 0.002 精确得 0，转成 double 还是 0.0 ⇒ B 门恒 False ⇒ 漏检。
--
-- 【修复】把 cast 下沉到**操作数**，令 SQL 全程 float64，与 Python 逐位一致：
--       (i.close::double precision - lag(i.close::double precision) OVER (...))
--   gap / cc / amp / idx_delta 四处同改；low_off 与 range 也转 double 以对齐
--   apply_positivity 的 float64 语义（该项默认不生效，仅保证口径一致）。
--
-- 【验证结论】（scripts/_verify_dp_fix.py，2026-10-01）
--   全市场 84 品种「15m 换月事件级」对拍：
--       ✗ 修复前（cast 在最外层转 double）：一致 78 / 不一致 6（还新增 RS888 多 2）
--       ✓ 修复后（操作数 cast）：一致 84 / 不一致 0，且 **gap 值逐位相同**
--
-- 【同批修复：INSERT 遗漏 n_bars（2026-10-01 全量重生成后验收发现）】
--   009 重写 sp 时 INSERT 列清单漏了 n_bars，导致重生成后 **n_bars 全表 NULL**
--   （旧快照有值，如 39976）⇒ 与旧表 EXCEPT 比对时 2886/2883/2881 行"全部不同"，
--   曾误报 full_table_diff=17774。本迁移补齐 fs.n_bars / ns.n_bars。
--   同时修 roll_ts：Python 原版 `roll_ts = ts[s] if k>0 else None`，
--   即**段 0 无换月、roll_ts 应为 NULL**；009 一律填了 seg_start，现改为 seg_no=0 时 NULL。
--   n_bars 与 roll_ts 已并入验收脚本逐项比对。
--
-- 【回滚】CREATE OR REPLACE 幂等；如需回到 009 行为，重新执行 009 文件即可。
--   L2 append-only，数据侧回退：DELETE FROM roll_segment WHERE freq=目标freq 后重跑。
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
BEGIN
    -- 显式传 NULL（如 CALL sp_build_l2_roll_segment('min5', NULL)）时兜底为 false。
    -- 否则 format('%L', NULL) 会拼出 "CASE WHEN NULL THEN sc.shift ELSE 0 END"——
    -- 语义不清且静默恒为 0（2026-10-01 重生成日志实测命中）。
    p_positivity := COALESCE(p_positivity, false);

    IF v_table IS NULL OR v_fallback IS NULL THEN
        RAISE EXCEPTION 'sp_build_l2_roll_segment: 不支持的 freq=%（仅 min5/15/30/60）', p_freq;
    END IF;
    -- 15m 锚点是硬依赖：缺失则无法锚定，拒绝执行（fail-loud）
    IF NOT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename=v_anchor) THEN
        RAISE EXCEPTION 'sp_build_l2_roll_segment: 锚点表 % 不存在，15m 锚定无法进行，拒绝执行', v_anchor;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename=v_table) THEN
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
        -- ⚠ 010 修复：所有四则运算的**操作数**先 ::double precision，与 Python float64
        --   逐位一致（不可写成 (a-b)::double precision —— 那是 numeric 精确算术先算完再转，
        --   会让 |Δ8888| == 0.40|cc| 的边界点错判为 False，实测漏检 TS/BB/FB/PM/WH 共 10 段）
        v_cte := format($c$
WITH anchor AS (
    SELECT m.bucket AS a_ts,
           (m.open::double precision
                - lag(m.close::double precision) OVER w) AS gap,
           (m.close::double precision
                - lag(m.close::double precision) OVER w) AS cc,
           ((m.high::double precision - m.low::double precision)
                + coalesce(lag(m.high::double precision - m.low::double precision) OVER w, 0)) AS amp,
           (i.close::double precision
                - lag(i.close::double precision) OVER (ORDER BY i.bucket)) AS idx_delta
    FROM bar_15m m
    LEFT JOIN bar_15m i ON i.bucket = m.bucket AND i.symbol = replace(m.symbol, '888', '8888')
    WHERE m.symbol = %1$L
    WINDOW w AS (ORDER BY m.bucket)
),
roll_events AS (
    SELECT a_ts, gap FROM anchor
    WHERE cc IS NOT NULL
      AND abs(cc) > (amp + 1e-6)
      AND coalesce(abs(idx_delta), 1e9) < 0.40 * abs(cc)
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
           CASE WHEN seg_no = 0 THEN 0::numeric
                ELSE round((first_delta)::numeric, 4) END AS roll_delta,
           -sum(CASE WHEN seg_no=0 THEN 0::numeric
                     ELSE round((first_delta)::numeric, 4) END)
               OVER (ORDER BY seg_no) AS cum_offset
    FROM seg_final
),
low_off AS (
    SELECT l.low::double precision + (
        SELECT s.cum_offset FROM full_segs s
        WHERE s.seg_start <= l.bucket AND (s.seg_end IS NULL OR l.bucket < s.seg_end)
        ORDER BY s.seg_start DESC LIMIT 1) AS v
    FROM %2$I l WHERE l.symbol = %1$L
),
shift_calc AS (
    SELECT CASE WHEN min(v) <= 0 THEN
        ceil((-min(v) + 0.01 * greatest((SELECT max(low)::double precision - min(low)::double precision
                                         FROM %2$I WHERE symbol=%1$L), 1.0)) / 100.0) * 100.0
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
                'roll_delta, cum_offset, n_bars, price_shift, src_freq, updated_at) '
                || v_cte ||
                ' SELECT %1$L, %2$L, fs.seg_no, fs.seg_start, fs.seg_end, '
                'CASE WHEN fs.seg_no = 0 THEN NULL ELSE fs.seg_start END, '
                'fs.roll_delta, fs.cum_offset, fs.n_bars, '
                'CASE WHEN %3$L THEN sc.shift ELSE 0 END, '
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
                'roll_delta, cum_offset, n_bars, price_shift, src_freq, updated_at) '
                || v_cte ||
                ', new_segs AS (SELECT *, row_number() OVER (ORDER BY seg_start) - 1 AS new_off '
                'FROM full_segs WHERE seg_start > %1$L::timestamptz) '
                'SELECT %2$L, %3$L, %4$L + 1 + ns.new_off, ns.seg_start, ns.seg_end, ns.seg_start, '
                'ns.roll_delta, (%5$L) - sum(ns.roll_delta) OVER (ORDER BY ns.seg_start), ns.n_bars, '
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
