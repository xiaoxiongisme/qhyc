-- =============================================================================
-- backfill_l0_meta.sql  ·  可选：回填 minute_bar 旧行（akshare 等）的分层/口径元数据
--
-- 背景：034_l0_minute_bar_extend.sql 只做 DDL（加列 + 索引），不改存量数据。
--       加列后存量 1.19 亿行的 variety_code / exchange 全为 NULL，任何按这两列过滤的
--       查询都会静默漏掉旧数据（例如 variety_code='RB' 只命中新 CSV 行）。
--
-- 特性：**一次性全表扫描**（119M 行 × 2 列），生产上预计数分钟，会产生表膨胀。
--       因此不放进 034，由运维显式执行：
--         psql "$PGURI" -v ON_ERROR_STOP=1 -f scripts/backfill_l0_meta.sql
--
-- 安全性：只更新 variety_code IS NULL 的行（幂等，可重复执行）；
--         不改任何价格/量/额数值，不改 layer（034 已给存量行默认 'continuous'）。
-- 校验：执行后按 (layer, variety_code) 统计行数，应与预期一致。
-- =============================================================================

DO $$
DECLARE
    v_base text;
    v_n    bigint;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='l0_raw' AND tablename='minute_bar') THEN
        v_base := 'l0_raw.minute_bar';
    ELSIF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='minute_bar') THEN
        v_base := 'public.minute_bar';
    ELSE
        RAISE EXCEPTION 'backfill: 找不到 minute_bar 实体表，请先跑 009 / 034';
    END IF;

    -- 1) 从 symbol 反推品种码：去掉末尾 888 / 8888
    --    RB888 → RB, RB8888 → RB, RB2701 → RB2701（无 888 后缀，保持原样，仅合约行不参与）
    EXECUTE format($f$
        UPDATE %s
           SET variety_code = regexp_replace(symbol, '88(88)?$', '')
         WHERE variety_code IS NULL
           AND symbol ~ '^[A-Za-z]+8888?$'
    $f$, v_base);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RAISE NOTICE '[backfill] 主连/指数行回填 variety_code: % 行', v_n;

    -- 2) exchange 从 l3_ref.dim_symbol 补（dim_symbol 是符号权威表）
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='l3_ref' AND tablename='dim_symbol') THEN
        EXECUTE format($f$
            UPDATE %s m
               SET exchange = ds.exchange
              FROM l3_ref.dim_symbol ds
             WHERE ds.symbol = m.symbol
               AND m.exchange IS NULL
               AND ds.exchange IS NOT NULL
        $f$, v_base);
        GET DIAGNOSTICS v_n = ROW_COUNT;
        RAISE NOTICE '[backfill] 按 dim_symbol 回填 exchange: % 行', v_n;
    ELSE
        RAISE NOTICE '[backfill] 跳过 exchange 回填：l3_ref.dim_symbol 不存在';
    END IF;

    -- 3) 剩余 exchange 为空的主连行，按品种码前缀硬映射（覆盖 dim_symbol 未登记的品种）
    EXECUTE format($f$
        UPDATE %s
           SET exchange = CASE
                 WHEN variety_code IN ('IC','IF','IH','IM','T','TF','TL','TS') THEN 'CFFEX'
                 WHEN variety_code IN ('SR','CF','TA','MA','FG','RM','OI','ZC','SM','SF','AP','CJ','UR','SA','PF','PK','PX','SH','CY','WH','PM','RI','RS','JR','LR') THEN 'CZCE'
                 WHEN variety_code IN ('A','B','C','CS','M','Y','P','I','J','JM','JD','L','V','PP','EG','EB','RR','LH','FB','BB') THEN 'DCE'
                 WHEN variety_code IN ('SC','NR','LU','BC','EC') THEN 'INE'
                 WHEN variety_code IN ('SI','LC','PS') THEN 'GFEX'
                 ELSE exchange
               END
         WHERE exchange IS NULL AND variety_code IS NOT NULL
    $f$, v_base);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RAISE NOTICE '[backfill] 按品种前缀兜底回填 exchange: % 行', v_n;
END $$;

-- 校验：分层 × 口径 × 品种覆盖
SELECT layer,
       count(*)                                   AS rows,
       count(*) FILTER (WHERE variety_code IS NULL) AS null_variety,
       count(*) FILTER (WHERE exchange IS NULL)     AS null_exchange,
       count(DISTINCT variety_code)                 AS n_variety
FROM l0_raw.minute_bar
GROUP BY layer
ORDER BY layer;

-- 校验：口径分布（应与设计一致：主连 raw / 合约 true 或 no_multiplier / 指数继承）
SELECT layer, caliber_vol, caliber_amt, count(*) AS rows
FROM l0_raw.minute_bar
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;
