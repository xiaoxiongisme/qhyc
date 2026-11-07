-- 053_tradable_scope.sql
-- 交易品种口径收口（CL-20261107-01 的 S2/S3/S4/S5 落地）
--
-- 【问题】2026-11-07 实测四套口径并存：
--     白名单 l3_ref.tradable_variety  38
--     cloud.yaml 主连配置              53（S1 已对齐，白名单全覆盖）
--     L1/L2 实际派生品种              82 主连 + 82 指数连
--     融合信号品种                     41（S1 已收口到扫描池 42）
--   L1 小时线 321 万行中，46.55%（149.6 万行）属于非白名单品种。
--   更严重的是这份白名单**没有任何代码/视图引用它**——建了表却零引用，
--   「白名单已生效」是一句没有任何证据支撑的话。
--
-- 【本迁移做什么】
--   1) 把「口径」定义落成**唯一一处** SQL：函数 l3_ref.tradable_scope_array()
--      + 视图 l3_ref.v_tradable_scope。Python（app/data/tradable_scope.py）
--      与存储过程、兼容视图全部只读这两者，杜绝再次各写一套。
--   2) L1 派生存储过程加品种过滤（S2 的 SQL 回滚路径）。
--   3) L2 后复权存储过程加品种过滤（S3）。
--   4) public.bar_5m/15m/30m/60m 与 access.v_bar_60m_main 兼容视图加过滤（S4）。
--   5) 建 l3_ref.nontradable_archive_log 月度归档水位线表（S5）。
--   6) 补齐 l3_ref.tradable_variety 的建表语句与种子（见下）。
--
-- 【口径的三段式定义】
--     派生/可见 = 有效白名单 ∪ 持仓豁免        （开关开启，默认）
--              = 全部已知品种                  （开关关闭 → 秒回退）
--
--   · **持仓豁免**不可省：改动前实测 B888/J888/LH888/NI888 四个非白名单品种
--     仍有持仓。若一刀切停派生，它们不再有新 bar，而止损/平仓判定完全依赖
--     最新 bar → 持仓被永久冻结在最后一个价格上。语义是「不再开新仓，
--     但存量持仓管理到平仓为止」。
--   · **fail-open** 不可省：口径视图读不到/为空时一律「不过滤」。宁可多算
--     无用品种，也不能因为一张参考表读不到就让整条数据链零产出。
--
-- 【为什么必须在迁移里补建 tradable_variety】
--   该表是 2026-11-07 手工建在生产库的，**从未进入 migrations**。
--   新环境按 migrations 重建时这张表不存在 → 口径视图建不起来 → 所有
--   依赖路径 fail-open → 「白名单生效」静默失效，而且没有任何报错。
--   建表语句用 IF NOT EXISTS + ON CONFLICT DO NOTHING，对生产零影响。
--
-- 【幂等】全部 CREATE OR REPLACE / IF NOT EXISTS，可重复执行。
--
-- 【回滚】UPDATE cfg_feature_switch SET enabled=false
--         WHERE switch_key='tradable_filter_enabled';   -- 秒级，无需改代码

BEGIN;

-- ===========================================================================
-- 0) 白名单表补齐（幂等；生产已存在，此处只为「可重建」）
-- ===========================================================================
CREATE SCHEMA IF NOT EXISTS l3_ref;

CREATE TABLE IF NOT EXISTS l3_ref.tradable_variety (
    variety_code   text NOT NULL PRIMARY KEY,
    display_name   text,
    effective_from date NOT NULL DEFAULT CURRENT_DATE,
    effective_to   date,
    note           text,
    created_at     timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE l3_ref.tradable_variety IS
    '真实可交易品种白名单（2026-11-07 用户拍板 38 个）。'
    '唯一真源：L1/L2 派生、兼容视图、融合信号扫描池全部读它。'
    '生效区间语义：effective_from <= CURRENT_DATE AND (effective_to IS NULL OR effective_to >= CURRENT_DATE)。';

INSERT INTO l3_ref.tradable_variety (variety_code, display_name, effective_from)
SELECT v, v, DATE '2026-11-06'
FROM unnest(ARRAY['A','AO','AP','BR','BU','C','CF','EB','EG','FG','FU','HC','I',
                  'JD','JM','L','M','MA','OI','P','PF','PK','PP','PX','RB','RM',
                  'SA','SF','SH','SI','SM','SP','SR','SS','TA','UR','V','Y']) AS v
ON CONFLICT (variety_code) DO NOTHING;

-- ===========================================================================
-- 1) 口径唯一真源：开关函数 + 集合函数 + 视图
-- ===========================================================================

-- 1.1 开关：表缺失/异常一律视为「开启」（与 app/scheduler.py::_switch_on 同语义，
--     但方向相反——这里 default=true，因为生产已确认要收口）
CREATE OR REPLACE FUNCTION l3_ref.tradable_filter_on() RETURNS boolean
LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF to_regclass('public.cfg_feature_switch') IS NULL THEN
        RETURN true;
    END IF;
    RETURN COALESCE(
        (SELECT enabled FROM public.cfg_feature_switch
          WHERE switch_key = 'tradable_filter_enabled'),
        true);            -- 键不存在 = 未配置 = 按默认收口（派生的默认行为）
EXCEPTION WHEN OTHERS THEN
    RETURN true;          -- 读开关报错不能反转成「全量派生」，那是静默放大
END $$;

COMMENT ON FUNCTION l3_ref.tradable_filter_on() IS
    'CL-20261107-01 总开关。false → 口径退化为「全部品种」，L1/L2 恢复全量派生、'
    '兼容视图恢复全量可见。秒级可回退，无需改代码/重启。';

-- 1.2 口径集合视图。返回「应派生/应可见」的完整 symbol（已含 888 / 8888 变体）。
--
--  ★ 性能：白名单展开成完整 symbol 数组后按 = ANY(array) 做线性扫描，
--    避免在 300 万行上逐行调用 regexp_replace 或标量函数（实测后者退化到分钟级）。
CREATE OR REPLACE VIEW l3_ref.v_tradable_scope AS
WITH cfg AS MATERIALIZED (
    SELECT
        l3_ref.tradable_filter_on() AS on_,
        -- 白名单 × {888, 8888}
        (SELECT array_agg(v.variety_code || x.sfx)
           FROM l3_ref.tradable_variety v
           CROSS JOIN (VALUES ('888'), ('8888')) AS x(sfx)
          WHERE v.effective_from <= CURRENT_DATE
            AND (v.effective_to IS NULL OR v.effective_to >= CURRENT_DATE)) AS wl,
        -- 持仓豁免：非 FLAT 的持仓品种，必须继续有行情才能被风控管理
        (SELECT array_agg(p.symbol)
           FROM public.fusion_position p
          WHERE COALESCE(p.position, 'FLAT') <> 'FLAT') AS held
)
SELECT DISTINCT s.symbol
FROM cfg
CROSS JOIN LATERAL unnest(
    -- 开关开启：白名单 ∪ 持仓豁免
    -- 开关关闭：空集 —— 消费方（Python / 存储过程）按「空 = 无法判定口径」
    --           一律 fail-open，即不过滤。把「不过滤」的定义留在消费方一处，
    --           视图不再自己拼一份全集，避免又长出第二套口径。
    CASE WHEN cfg.on_
         THEN COALESCE(cfg.wl, ARRAY[]::text[])
              || COALESCE(cfg.held, ARRAY[]::text[])
         ELSE ARRAY[]::text[] END
) AS s(symbol);

COMMENT ON VIEW l3_ref.v_tradable_scope IS
    'CL-20261107-01 口径唯一真源：白名单 ∪ 持仓豁免。'
    '开关 tradable_filter_enabled=false 时视图为空（= 调用方 fail-open，不过滤）。'
    'Python 侧 app/data/tradable_scope.py、存储过程、兼容视图全部只读这一个来源。';

-- 1.3 便利函数：一次取回数组，供存储过程 / 脚本使用。
--     返回 NULL = 无法判定口径 → 调用方必须 fail-open（不过滤）。
CREATE OR REPLACE FUNCTION l3_ref.tradable_scope_array() RETURNS text[]
LANGUAGE plpgsql STABLE AS $$
DECLARE v text[];
BEGIN
    IF to_regclass('l3_ref.v_tradable_scope') IS NULL THEN
        RETURN NULL;
    END IF;
    BEGIN
        EXECUTE 'SELECT array_agg(symbol) FROM l3_ref.v_tradable_scope' INTO v;
    EXCEPTION WHEN OTHERS THEN
        RETURN NULL;      -- 视图定义漂移/表缺失 → fail-open
    END;
    IF v IS NULL OR cardinality(v) = 0 THEN
        RETURN NULL;      -- 空集合 → fail-open（避免零产出）
    END IF;
    RETURN v;
END $$;

COMMENT ON FUNCTION l3_ref.tradable_scope_array() IS
    '返回口径数组；NULL 表示「无法判定」→ 调用方必须 fail-open。';

-- ===========================================================================
-- 2) S2：L1 派生加品种过滤（白名单 ∪ 持仓，fail-open）
--    ★ 生产当前走 Python 合成（ingest.bar_synth_mode=python），本过程是
--      「切回 sp 模式」的回滚路径。两条路径必须同口径，否则切模式即口径漂移。
--    ★ 顺带修复：原 INSERT 列集漏 amount，sp 模式下 amount 恒 NULL（与
--      Python 路径不一致）。此处补 sum(amount)。
-- ===========================================================================
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
    v_chunk     interval := interval '1 day';
    v_n_chunk   int := 0;
    v_scope     text[];
    v_scope_on  boolean := false;
BEGIN
    IF v_table IS NULL OR v_interval IS NULL THEN
        RAISE EXCEPTION 'sp_build_l1_from_minute: 不支持的 freq=%（仅 min5/15/30/60）', p_freq;
    END IF;
    IF to_regclass('public.' || v_src) IS NULL THEN
        RAISE NOTICE '[L1] 源表 % 不存在，跳过', v_src; RETURN;
    END IF;

    -- ---- 品种口径（CL-20261107-01 S2） ----
    v_scope := l3_ref.tradable_scope_array();
    IF v_scope IS NULL THEN
        v_scope_on := false;
        RAISE WARNING '[L1] 口径视图不可用/为空 → 本轮全品种派生（fail-open）';
    ELSE
        v_scope_on := true;
        RAISE NOTICE '[L1] 品种口径生效：% 个 symbol（白名单 ∪ 持仓豁免）', cardinality(v_scope);
    END IF;

    -- 范围确定
    IF p_from IS NOT NULL THEN
        v_from := p_from;
        IF p_to IS NOT NULL THEN
            v_to := p_to;
        ELSE
            SELECT time_bucket(v_interval, max(ts)) INTO v_to FROM minute_bar;
        END IF;
    ELSE
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

    v_cur := v_from;
    LOOP
        EXIT WHEN v_cur >= v_to;
        v_end := least(v_cur + v_chunk, v_to);
        EXECUTE format($q$
            INSERT INTO %I (symbol, bucket, open, high, low, close, volume, amount, open_interest)
            SELECT symbol,
                   time_bucket($1, ts) AS bucket,
                   first(open, ts)  AS open,
                   max(high)        AS high,
                   min(low)         AS low,
                   last(close, ts)  AS close,
                   sum(volume)             AS volume,
                   sum(amount)             AS amount,
                   max(open_interest)      AS open_interest
            FROM minute_bar
            WHERE symbol ~ '^[A-Za-z]+8888?$' AND ts >= $2 AND ts < $3
              AND (NOT $4 OR symbol = ANY($5))
            GROUP BY symbol, time_bucket($1, ts)
            ON CONFLICT (symbol, bucket) DO UPDATE
               SET open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                   close=EXCLUDED.close, volume=EXCLUDED.volume,
                   amount=EXCLUDED.amount, open_interest=EXCLUDED.open_interest
            $q$, v_table)
            USING v_interval, v_cur, v_end, v_scope_on, v_scope;
        v_n_chunk := v_n_chunk + 1;
        v_cur := v_end;
    END LOOP;

    EXECUTE format('SELECT count(*) FROM %I', v_table) INTO v_dst_n;
    RAISE NOTICE '[L1] % 增量/分块聚合完成：% 天块，目标表现有 % 行', v_table, v_n_chunk, v_dst_n;
END;
$$;

COMMENT ON PROCEDURE sp_build_l1_from_minute(text, timestamptz, timestamptz) IS
    'L0 分钟 → L1 多周期聚合。分块避免 TimescaleDB 解压上限/OOM。'
    'CL-20261107-01 S2：仅派生「白名单 ∪ 持仓豁免」品种，口径读 '
    'l3_ref.tradable_scope_array()，不可用时 fail-open 全量派生。'
    '053 起补 amount 列（此前 sp 模式 amount 恒 NULL）。';

-- ===========================================================================
-- 3) S3：L2 后复权加品种过滤（白名单 ∪ 持仓，fail-open）
--    原逻辑对 v_src 里出现的**全部** 888 主连做 as-of 复权，含 44 个非白名单
--    品种。这里把循环集合收口到口径内；roll_segment 是 append-only 参考表，
--    不加过滤（非白名单品种的段保留，改白名单后可立即复用，无需重算）。
-- ===========================================================================
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
                'FROM %s b %s WHERE b.symbol=%L AND b.bucket > %L::timestamptz '
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
    'l3_ref.tradable_scope_array()，不可用时 fail-open 全量复权。';

-- ===========================================================================
-- 4) S4：兼容视图加品种过滤
--
--    ★ 为什么改视图而不是只停派生：停派生只阻止**新增**，历史残留的 149.6 万行
--      非白名单数据仍在表里，任何 `SELECT ... FROM bar_60m` 依然读得到。
--      而 search_path = ("$user", public)，裸表名 bar_60m 命中的正是这里的视图
--      （app/data/caliber.py 的路由就写的是裸名）→ 收口必须落在视图上，
--      否则「品种池」在读取层永远是 82。
--    ★ 原始全量数据保留在 l1_mkt.bar_*，研究/回补需要时显式限定 schema 即可。
--    ★ 性能（2026-11-07 实测，321 万行）与为什么这样写：
--      开关用 initplan：`NOT (SELECT ...)` 只求值一次；
--      成员判断用 `IN (子查询)` 走 hashed SubPlan，每行一次 hash 探测。
--      首版写成 MATERIALIZED CTE + `= ANY(array)`，76 元数组逐行线性扫描，
--      321 万行 = 2.4 亿次比较：count(*) 4660ms → 现在 679ms（6.9x）；
--      带时间窗聚合 58ms → 7ms（8x）。**不要改回 = ANY(array)**。
-- ===========================================================================
CREATE OR REPLACE VIEW public.bar_5m AS
SELECT b.symbol, b.bucket, b.open, b.high, b.low, b.close,
       b.volume, b.amount, b.open_interest
FROM l1_mkt.bar_5m b
WHERE NOT (SELECT l3_ref.tradable_filter_on())
   OR b.symbol IN (SELECT symbol FROM l3_ref.v_tradable_scope);

CREATE OR REPLACE VIEW public.bar_15m AS
SELECT b.symbol, b.bucket, b.open, b.high, b.low, b.close,
       b.volume, b.amount, b.open_interest
FROM l1_mkt.bar_15m b
WHERE NOT (SELECT l3_ref.tradable_filter_on())
   OR b.symbol IN (SELECT symbol FROM l3_ref.v_tradable_scope);

CREATE OR REPLACE VIEW public.bar_30m AS
SELECT b.symbol, b.bucket, b.open, b.high, b.low, b.close,
       b.volume, b.amount, b.open_interest
FROM l1_mkt.bar_30m b
WHERE NOT (SELECT l3_ref.tradable_filter_on())
   OR b.symbol IN (SELECT symbol FROM l3_ref.v_tradable_scope);

CREATE OR REPLACE VIEW public.bar_60m AS
SELECT b.symbol, b.bucket, b.open, b.high, b.low, b.close,
       b.volume, b.amount, b.open_interest
FROM l1_mkt.bar_60m b
WHERE NOT (SELECT l3_ref.tradable_filter_on())
   OR b.symbol IN (SELECT symbol FROM l3_ref.v_tradable_scope);

COMMENT ON VIEW public.bar_60m IS
    '60m 未复权主连兼容视图（CL-20261107-01 S4 起按品种口径过滤）。'
    '口径 = 白名单 ∪ 持仓豁免；开关 tradable_filter_enabled=false 时全量可见。'
    '需要未过滤的全量历史请显式查询 l1_mkt.bar_60m。';

-- access 层同口径（供 BI/外部只读账号）
CREATE OR REPLACE VIEW access.v_bar_60m_main AS
SELECT b.symbol, b.bucket, b.open, b.high, b.low, b.close,
       b.volume, b.amount, b.open_interest
FROM l1_mkt.bar_60m b
WHERE NOT (SELECT l3_ref.tradable_filter_on())
   OR b.symbol IN (SELECT symbol FROM l3_ref.v_tradable_scope);

-- ===========================================================================
-- 5) S5：月度归档水位线表
--    方案 v3 / Q5-Q9 定案：**只导出不删**（D1），每月 1 日 03:20 把「非白名单
--    历史」按月增量导出，水位线记录已导出到哪个 ts，可断点续跑。
--    首次 baseline（302 片 / 2.0 亿行 / 3.65GB，cutoff 2026-11-06 15:00）
--    已单独完成，此表从 2026-11-06 15:00 起记水位，避免重复导出。
-- ===========================================================================
CREATE TABLE IF NOT EXISTS l3_ref.nontradable_archive_log (
    id            bigserial PRIMARY KEY,
    table_name    text NOT NULL,
    batch_key     text NOT NULL,          -- 月度批标识，如 '2026-12'
    watermark_ts  timestamptz,            -- 本轮导出覆盖到的时间上界（下一轮起点）
    rows_exported bigint NOT NULL DEFAULT 0,
    file_path     text,
    sha256        text,
    status        text NOT NULL DEFAULT 'running',
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    note          text,
    CONSTRAINT nontradable_archive_log_status_chk
        CHECK (status IN ('running', 'success', 'failed', 'partial')),
    CONSTRAINT nontradable_archive_log_uniq UNIQUE (table_name, batch_key)
);

COMMENT ON TABLE l3_ref.nontradable_archive_log IS
    'CL-20261107-01 S5：非白名单历史月度归档水位线。'
    '每月 1 日 03:20 由 scripts/ops/archive_nontradable.py 增量导出；'
    '(table_name, batch_key) 唯一，断点续跑时先查已 success 的 batch_key 跳过。'
    'D1 决策：只导出不删（观察 1 个月后再议删除）。';

-- 5.1 登记调度字典（否则 resync 会把未登记的作业移除/按代码默认漂移）
INSERT INTO ops.scheduled_task
    (task_id, name, purpose, executor, schedule, schedule_desc,
     target, location, enabled, expected_gap_days, owner, tags, meta)
VALUES (
    'archive_nontradable',
    '非白名单历史月度归档',
    'CL-20261107-01 S5：每月 1 日把「上次水位 → 现在」的非白名单行增量导出到'
    '/Docker/qhyc_archive/nontradable（L0 分钟含 index + L1 + L2），水位线落 '
    'l3_ref.nontradable_archive_log，只导出不删（D1）。',
    'apscheduler',
    '20 3 1 * *',
    '每月 1 日 03:20',
    '/Docker/qhyc_archive/nontradable',
    'cloud',
    true,
    NULL,
    'data-platform',
    ARRAY['data', 'archive', 'tradable-scope'],
    '{"script": "scripts/ops/archive_nontradable.py", "delete": false, "ledger": "CL-20261107-01"}'::jsonb
)
ON CONFLICT (task_id) DO NOTHING;

COMMIT;
