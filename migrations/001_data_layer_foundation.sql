-- =============================================================================
-- 001 · 数据层地基：dim_symbol 维度表 + 命名空间治理 + 补齐唯一性
-- 日期：2026-09-29　Phase 1（六层解耦 · 数据层）
-- 适用：本地库与云端库同构执行（幂等，可重复运行）
--
-- 背景（2026-09-28 实测）
--   1. 全库 39 张业务表**零外键**，关联靠口头约定；
--   2. symbol 存在 4 套命名空间并存：
--        A888        主连（合成连续，未复权）        ← min15/30/60、bar_*
--        A8888       指数连（不可直接交易）          ← 复权对照
--        KQ.m@DCE.A  天勤原生连续码                  ← daily/hourly cont_adj 主流
--        AP2701      标准合约（品种大写+YYMM）        ← contract_daily
--      导致「跨周期 join 静默少数据」（PRD 差距分析已点名）；
--   3. minute_bar_stage 是全库唯一无主键的表（当前 0 行）。
--
-- 设计取舍（性能优先，不做过度约束）
--   * 建 dim_symbol 维度表做映射，**不物理删除**重叠数据：
--     实测 KQ.m@CZCE.FG(18,866) 与 FG888(72,192) 仅 3,852 行时间戳重合，
--     删除会丢 15,014 行。改用 is_preferred 标记首选命名空间。
--   * 亿级行情表（minute_bar 1.15亿 / bar_5m 2412万 / fut_kline 2522万）
--     **不加外键**：每次 INSERT 都要回查父表，写入放大不可接受。
--     改为 dim_symbol 映射 + 定期孤儿巡检（scripts/db_quality_audit.py）。
--   * 外键只加在**中低频表**（因子/回测/预测），父表极小可常驻内存。
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. dim_symbol：symbol 维度表（唯一权威字典）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_symbol (
    symbol        text PRIMARY KEY,
    product       text NOT NULL,              -- 品种代码：A / FG / AP
    namespace     text NOT NULL,              -- main | index | contract | tqsdk | other
    exchange      text,                       -- 交易所：DCE/CZCE/SHFE/CFFEX/GFEX/INE
    is_preferred  boolean NOT NULL DEFAULT false,  -- 同 (product, namespace) 内的首选符号
    first_seen    date,
    last_seen     date,
    source_tables text[] NOT NULL DEFAULT '{}',
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_dim_symbol_product ON dim_symbol(product, namespace);
CREATE INDEX IF NOT EXISTS ix_dim_symbol_pref ON dim_symbol(product, namespace, is_preferred)
    WHERE is_preferred;

COMMENT ON TABLE dim_symbol IS
    'symbol 维度表（数据层唯一字典）。namespace: main=888主连 / index=8888指数连 / '
    'contract=标准合约 / tqsdk=KQ.m@天勤原生码。跨周期 join 必须先经本表映射。';

-- ---------------------------------------------------------------------------
-- 2. 回填：从各行情/因子表收集 DISTINCT symbol 并解析命名空间
--    规则（有优先级，先匹配先生效）：
--      ^KQ\.m@([A-Z]+)\.([A-Z0-9]+)$  → tqsdk，exchange=$1，product=$2
--      ^([A-Za-z0-9]+)8888$           → index，product=$1
--      ^([A-Za-z]+)888$               → main，  product=$1
--      ^([A-Za-z]+)(\d{4})$           → contract，product=$1
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION dim_symbol_parse(s text)
RETURNS TABLE(product text, namespace text, exchange text) AS $$
DECLARE
    m text[];
BEGIN
    -- 天勤原生连续码：KQ.m@DCE.A
    IF s ~ '^KQ\.m@[A-Z]+\.[A-Z0-9]+$' THEN
        m := regexp_match(s, '^KQ\.m@([A-Z]+)\.([A-Z0-9]+)$');
        RETURN QUERY SELECT upper(m[2]), 'tqsdk'::text, m[1];
    -- 指数连：A8888
    ELSIF s ~ '^[A-Za-z0-9]+8888$' THEN
        m := regexp_match(s, '^([A-Za-z0-9]+)8888$');
        RETURN QUERY SELECT upper(m[1]), 'index'::text, NULL::text;
    -- 主连：A888
    ELSIF s ~ '^[A-Za-z]+888$' THEN
        m := regexp_match(s, '^([A-Za-z]+)888$');
        RETURN QUERY SELECT upper(m[1]), 'main'::text, NULL::text;
    -- 标准合约：AP2701
    ELSIF s ~ '^[A-Za-z]+[0-9]{4}$' THEN
        m := regexp_match(s, '^([A-Za-z]+)([0-9]{4})$');
        RETURN QUERY SELECT upper(m[1]), 'contract'::text, NULL::text;
    ELSE
        RETURN QUERY SELECT upper(s), 'other'::text, NULL::text;
    END IF;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- ---------------------------------------------------------------------------
-- 2. 从各表收集 symbol（逐表插入以记录来源）
--
-- ⚠ 性能/稳定性要点（2026-09-29 实测，两边都踩过）：
--    超表不带时间条件做 SELECT DISTINCT 会让 planner 打开**全部 chunk**，
--    每个 chunk 一把 AccessShareLock；云端 max_locks_per_transaction 仍是
--    默认 64，而 fut_kline 有 1133 个 chunk → 直接
--      ERROR: out of shared memory / You might need to increase
--             max_locks_per_transaction
--    因此对超表一律**按时间窗口分片扫描**（季度窗口，实测单窗口 ≤30 chunk），
--    普通表仍走一次性 DISTINCT。这样本迁移在「没调过锁参数」的库上也能跑。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION dim_symbol_collect(t text, col text, tc text)
RETURNS void AS $$
DECLARE
    w_start timestamptz := '2010-01-01'::timestamptz;
    w_end   timestamptz;
    head    text := format($h$
        INSERT INTO dim_symbol (symbol, product, namespace, exchange, source_tables)
        SELECT s.sym, p.product, p.namespace, p.exchange, ARRAY[%L]
        FROM (SELECT DISTINCT %I AS sym FROM public.%I $h$, t, col, t);
    tail    text := format($h$
             ) s CROSS JOIN LATERAL dim_symbol_parse(s.sym) p
        ON CONFLICT (symbol) DO UPDATE
           SET source_tables = (
                SELECT ARRAY(SELECT DISTINCT unnest(dim_symbol.source_tables || EXCLUDED.source_tables))
               ),
               updated_at = now() $h$);
BEGIN
    IF tc IS NULL THEN
        EXECUTE head || format('WHERE %I IS NOT NULL', col) || tail;
        RETURN;
    END IF;

    WHILE w_start < now() + interval '3 months' LOOP
        w_end := w_start + interval '3 months';
        EXECUTE head
             || format('WHERE %I IS NOT NULL AND %I >= %L AND %I < %L', col, tc, w_start, tc, w_end)
             || tail;
        w_start := w_end;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    t  text;
    col text;
    tc text;
    -- 表 -> 时间列（超表/时间分区表必填，NULL 表示普通表走一次性 DISTINCT）
    tc_map jsonb := '{
        "fut_kline": "trade_datetime", "minute_bar": "ts", "minute_bar_adj": "ts",
        "hourly_bar": "trade_datetime", "daily_bar": "trade_date",
        "bar_5m": "bucket", "bar_15m": "bucket", "bar_30m": "bucket", "bar_60m": "bucket",
        "sector_index": "trade_date", "contract_daily": "trade_date",
        "main_continuous": "trade_date"
    }'::jsonb;
    tables text[] := ARRAY[
        'fut_kline', 'bar_5m', 'bar_15m', 'bar_30m', 'bar_60m',
        'minute_bar', 'minute_bar_adj', 'hourly_bar', 'daily_bar',
        'contract_daily', 'main_continuous', 'factor_value',
        'fusion_position', 'spot_basis', 'warehouse_receipt',
        'member_position_rank', 'inventory', 'roll_yield',
        'futures_symbol', 'sector_map'
    ];
BEGIN
    FOREACH t IN ARRAY tables LOOP
        IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename=t) THEN
            -- 少数表只有 product 列没有 symbol 列（2026-09-29 实测：main_continuous / sector_map）
            SELECT CASE
                     WHEN EXISTS (SELECT 1 FROM information_schema.columns
                                  WHERE table_schema='public' AND table_name=t
                                    AND column_name='symbol') THEN 'symbol'
                     ELSE 'product'
                   END INTO col;
            -- 时间列只有在表里真实存在时才启用分片扫描（防御 schema 差异）
            tc := NULL;
            IF tc_map ? t
               AND EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_schema='public' AND table_name=t
                             AND column_name = (tc_map->>t)) THEN
                tc := tc_map ->> t;
            END IF;
            PERFORM dim_symbol_collect(t, col, tc);
            RAISE NOTICE 'dim_symbol 已从 % 回填（列=%，时间列=%）', t, col, coalesce(tc, '-');
        END IF;
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 3. is_preferred：同一 (product, namespace) 内只留一个首选
--    规则：main/index/contract 天然唯一；tqsdk 与 main 并存时 prefer main。
--    实测冲突样例：FG → FG888(72,192 行, 2015~) 优于 KQ.m@CZCE.FG(18,866 行, 2023~)
-- ---------------------------------------------------------------------------
UPDATE dim_symbol SET is_preferred = false;

UPDATE dim_symbol d SET is_preferred = true
WHERE d.namespace IN ('main', 'index', 'contract')
  AND NOT EXISTS (
      SELECT 1 FROM dim_symbol o
      WHERE o.product = d.product AND o.namespace = d.namespace
        AND (o.symbol < d.symbol)
  );

-- tqsdk：仅当该 product 没有 main 命名空间时才作首选
UPDATE dim_symbol d SET is_preferred = true
WHERE d.namespace = 'tqsdk'
  AND NOT EXISTS (SELECT 1 FROM dim_symbol o
                  WHERE o.product = d.product AND o.namespace = 'main');

-- ---------------------------------------------------------------------------
-- 4. 便捷视图：品种 → 各命名空间符号（上层取数唯一入口的辅助）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_symbol_canonical AS
SELECT product,
       max(symbol) FILTER (WHERE namespace='main'     AND is_preferred) AS main_symbol,
       max(symbol) FILTER (WHERE namespace='index'    AND is_preferred) AS index_symbol,
       max(symbol) FILTER (WHERE namespace='contract' AND is_preferred) AS contract_symbol,
       max(symbol) FILTER (WHERE namespace='tqsdk'    AND is_preferred) AS tqsdk_symbol,
       count(*) AS symbol_count,
       count(*) FILTER (WHERE namespace='tqsdk') > 0
         AND count(*) FILTER (WHERE namespace='main') > 0 AS has_namespace_conflict
FROM dim_symbol
GROUP BY product;

COMMENT ON VIEW v_symbol_canonical IS
    '品种 → 各命名空间首选符号。has_namespace_conflict=true 表示该品种同时存在 '
    '888 与 KQ.m@ 两套码，跨周期 join 前必须显式指定取哪个（BarStore 默认取 main）。';

-- ---------------------------------------------------------------------------
-- 5. 补主键：minute_bar_stage（全库唯一无 PK 的表，当前 0 行）
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='minute_bar_stage') THEN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint
                       WHERE conrelid='public.minute_bar_stage'::regclass AND contype='p') THEN
            -- 先清重复再建（若有）
            DELETE FROM public.minute_bar_stage a
             USING public.minute_bar_stage b
             WHERE a.ctid > b.ctid
               AND a.symbol IS NOT DISTINCT FROM b.symbol
               AND a.ts IS NOT DISTINCT FROM b.ts;
            EXECUTE 'ALTER TABLE public.minute_bar_stage ADD PRIMARY KEY (symbol, ts)';
            RAISE NOTICE 'minute_bar_stage 主键已建立';
        END IF;
    END IF;
END $$;

COMMIT;
