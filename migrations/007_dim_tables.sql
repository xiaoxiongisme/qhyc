-- =============================================================================
-- 007 · 结构型字典表：dim_exchange / dim_variety / dim_contract
-- 日期：2026-09-30　数据层重构（用户要求：品种/代码/合约统一字典 + 合约配置表）
-- 适用：本地库与云端库同构执行（幂等，可重复运行）
--
-- 设计目标
--   * 把「期货品种 / 交易所 / 合约生命周期」从散落在各采集脚本的硬编码里
--     抽出来，成为**唯一权威字典**。上层（采集 / 回测 / 因子 / 信号）只认
--     variety_code + contract_code，所有 888/8888/KQ.m@ 转换由 dim_symbol +
--     dim_contract + barstore.resolve_* 集中完成（见 001 / barstore.py）。
--   * 满足：「合约配置表，诸如每个合约的起止时间、合约是否主连」。
--
-- 取舍（性能优先，沿用 001 的纪律）
--   * dim_* 都是小表（几百~几千行），可加 FK（父表极小，写入放大可忽略）；
--     亿级行情表（bar_*/fut_kline）**仍不加 FK**（见 001 头部说明）。
--   * 仅做「结构化落盘 + 回填」，不删除任何既有数据。
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. dim_exchange：交易所字典
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_exchange (
    exchange_code  text PRIMARY KEY,                 -- SHFE / DCE / CZCE / CFFEX / INE / GFEX
    exchange_name  text NOT NULL,
    full_name      text,
    currency       text NOT NULL DEFAULT 'CNY',
    has_night      boolean NOT NULL DEFAULT true,    -- 是否有夜盘
    note           text,
    updated_at     timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE dim_exchange IS
    '交易所字典（数据层唯一权威）。品种/合约的交易所维度统一引用本表。';

INSERT INTO dim_exchange (exchange_code, exchange_name, full_name, has_night)
VALUES
    ('SHFE',  '上期所', '上海期货交易所',          true),
    ('DCE',   '大商所', '大连商品交易所',          true),
    ('CZCE',  '郑商所', '郑州商品交易所',          true),
    ('CFFEX', '中金所', '中国金融期货交易所',      false),
    ('INE',   '上期能源', '上海国际能源交易中心',  true),
    ('GFEX',  '广期所', '广州期货交易所',          true),
    -- 2026-10-01 勘误：云端 dim_symbol 存在 exchange='XX' 的采集占位 10 行
    -- （BB/FB/JR/LG/LR/ME/PM/RI/TC/ZC，KQ.m@XX.*，冷门/退市品种未识别交易所）。
    -- dim_variety 回填链会把该值带进来，若无父行则 FK 违反、整个迁移回滚（云端实测）。
    ('XX',    '未知',   '未识别交易所（采集占位）', false)
ON CONFLICT (exchange_code) DO UPDATE
    SET exchange_name = EXCLUDED.exchange_name,
        full_name     = COALESCE(EXCLUDED.full_name, dim_exchange.full_name),
        has_night     = EXCLUDED.has_night,
        updated_at    = now();

-- ---------------------------------------------------------------------------
-- 2. dim_variety：品种字典（合并自 futures_symbol 73 行 + dim_symbol）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_variety (
    variety_code   text PRIMARY KEY,                 -- 品种码：CU / AU / FG / AP
    variety_name   text,                              -- 沪铜 / 黄金 / 玻璃 / 苹果
    exchange       text REFERENCES dim_exchange(exchange_code),
    sector         text,                             -- 板块：有色/贵金属/黑色/化工/农产品/金融
    tick_size      numeric(20,6),                    -- 最小变动价位
    multiplier     numeric(20,4),                    -- 合约乘数（元/点）
    quote_unit     text,                             -- 报价单位（元/吨）
    main_symbol    text,                             -- 所属主连：CU888
    is_active      boolean NOT NULL DEFAULT true,
    source         text,                             -- 回填来源审计
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_dim_variety_exchange ON dim_variety(exchange);
CREATE INDEX IF NOT EXISTS ix_dim_variety_sector   ON dim_variety(sector);

COMMENT ON TABLE dim_variety IS
    '品种字典（数据层唯一权威）。上层只传 variety_code，其余属性在此查。';
COMMENT ON COLUMN dim_variety.main_symbol IS
    '该品种对应的主连符号（如 CU888）。优先取自 futures_symbol.main_symbol。';

-- 回填：以 futures_symbol（品种主表 73 行）为主，dim_symbol 的 product 为辅
INSERT INTO dim_variety
    (variety_code, variety_name, exchange, tick_size, multiplier,
     quote_unit, main_symbol, is_active, source)
SELECT
    fs.symbol,
    fs.name,
    NULLIF(fs.exchange, ''),
    fs.multiplier,
    fs.multiplier,
    fs.unit,
    fs.main_symbol,
    fs.active,
    'futures_symbol'
FROM futures_symbol fs
WHERE fs.symbol IS NOT NULL
ON CONFLICT (variety_code) DO UPDATE
    SET variety_name = COALESCE(EXCLUDED.variety_name, dim_variety.variety_name),
        exchange     = COALESCE(EXCLUDED.exchange, dim_variety.exchange),
        tick_size    = COALESCE(EXCLUDED.tick_size, dim_variety.tick_size),
        multiplier   = COALESCE(EXCLUDED.multiplier, dim_variety.multiplier),
        quote_unit   = COALESCE(EXCLUDED.quote_unit, dim_variety.quote_unit),
        main_symbol  = COALESCE(EXCLUDED.main_symbol, dim_variety.main_symbol),
        is_active    = COALESCE(EXCLUDED.is_active, dim_variety.is_active),
        source       = 'futures_symbol',
        updated_at   = now();

-- 补齐：dim_symbol 里出现、但 futures_symbol 没有的品种（产品码）
INSERT INTO dim_variety (variety_code, variety_name, exchange, is_active, source)
SELECT DISTINCT d.product, NULL, NULL, true, 'dim_symbol'
FROM dim_symbol d
WHERE d.product IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM dim_variety v WHERE v.variety_code = d.product)
ON CONFLICT (variety_code) DO NOTHING;

-- 回填 exchange：优先 contract_code_map，再 dim_symbol.天勤 命名空间
UPDATE dim_variety dv
   SET exchange = COALESCE(
        (SELECT ccm.exchange FROM contract_code_map ccm
          WHERE ccm.product = dv.variety_code LIMIT 1),
        (SELECT d.exchange FROM dim_symbol d
          WHERE d.product = dv.variety_code AND d.exchange IS NOT NULL LIMIT 1)),
       updated_at = now()
 WHERE dv.exchange IS NULL;

-- ---------------------------------------------------------------------------
-- 3. dim_contract：合约配置表（用户要求：起止时间 + 是否主连）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_contract (
    contract_code     text PRIMARY KEY,               -- 标准码 4 位：AP2701 / CU2611
    variety_code      text REFERENCES dim_variety(variety_code),
    exchange          text REFERENCES dim_exchange(exchange_code),
    product_name      text,                           -- 品种中文名（别名）
    delivery_year     int,                            -- 交割年
    delivery_month    int,                            -- 交割月 1~12
    contract_ym       text,                           -- 交割年月文本 YYYYMM（排序用）
    list_date         date,                           -- 上市日（取自 contract_daily 最早交易日）
    first_trade_date  date,
    last_trade_date   date,                           -- 最后交易日（contract_daily 最晚交易日）
    delivery_start    date,
    delivery_end      date,
    main_symbol       text,                           -- 所属主连：CU888
    is_main           boolean NOT NULL DEFAULT false, -- 是否当前/近期主力合约（回填时快照）
    is_active         boolean NOT NULL DEFAULT true,  -- 是否仍在交易
    source            text,
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_dim_contract_variety ON dim_contract(variety_code);
CREATE INDEX IF NOT EXISTS ix_dim_contract_ym      ON dim_contract(contract_ym);
CREATE INDEX IF NOT EXISTS ix_dim_contract_main    ON dim_contract(is_main) WHERE is_main;

COMMENT ON TABLE dim_contract IS
    '合约配置表（数据层唯一权威）。每个合约的上市/交割起止、是否主力、所属主连。'
    '上层只传 contract_code，所有 888/8888/KQ.m@ 转换由 dim_symbol/dim_contract 完成。';
COMMENT ON COLUMN dim_contract.is_main IS
    '回填快照：该合约是否出现在 main_contract_map 最新一日 underlying（即当前主力）。'
    '实时「当前主力」由 main_contract_map + 日期推导，本列仅作静态标记。';

-- 3.1 回填：来自 contract_code_map（std_symbol / product / exchange / 交割年/月 / name）
INSERT INTO dim_contract
    (contract_code, variety_code, exchange, product_name,
     delivery_year, delivery_month, contract_ym, main_symbol, is_active, source)
SELECT DISTINCT
    ccm.std_symbol,
    ccm.product,
    ccm.exchange,
    ccm.name,
    ccm.deliv_year,
    ccm.deliv_month,
    to_char(ccm.deliv_year * 100 + ccm.deliv_month, 'FM000000'),
    (SELECT fs.main_symbol FROM futures_symbol fs
      WHERE fs.symbol = ccm.product OR fs.product = ccm.product LIMIT 1),
    true,
    'contract_code_map'
FROM contract_code_map ccm
ON CONFLICT (contract_code) DO UPDATE
    SET variety_code   = COALESCE(EXCLUDED.variety_code, dim_contract.variety_code),
        exchange       = COALESCE(EXCLUDED.exchange, dim_contract.exchange),
        product_name   = COALESCE(EXCLUDED.product_name, dim_contract.product_name),
        delivery_year  = COALESCE(EXCLUDED.delivery_year, dim_contract.delivery_year),
        delivery_month = COALESCE(EXCLUDED.delivery_month, dim_contract.delivery_month),
        contract_ym    = COALESCE(EXCLUDED.contract_ym, dim_contract.contract_ym),
        main_symbol    = COALESCE(EXCLUDED.main_symbol, dim_contract.main_symbol),
        updated_at     = now();

-- 3.2 回填：dim_symbol 的 contract 命名空间（标准合约如 AP2701）若上面漏了
INSERT INTO dim_contract
    (contract_code, variety_code, exchange, delivery_year, delivery_month,
     contract_ym, is_active, source)
SELECT DISTINCT
    d.symbol,
    d.product,
    NULL,
    CASE WHEN d.symbol ~ '^[A-Za-z]+\d{4}$'
         THEN substring(d.symbol from '\d{2}')::int + 2000 END,
    CASE WHEN d.symbol ~ '^[A-Za-z]+\d{4}$'
         THEN substring(d.symbol from '\d{2}$')::int END,
    CASE WHEN d.symbol ~ '^[A-Za-z]+\d{4}$'
         THEN to_char(
            (substring(d.symbol from '\d{2}')::int + 2000) * 100
            + substring(d.symbol from '\d{2}$')::int, 'FM000000') END,
    true,
    'dim_symbol'
FROM dim_symbol d
WHERE d.namespace = 'contract'
  AND d.symbol ~ '^[A-Za-z]+\d{4}$'
  AND NOT EXISTS (SELECT 1 FROM dim_contract c WHERE c.contract_code = d.symbol)
ON CONFLICT (contract_code) DO NOTHING;

-- 3.3 回填生命周期日期：来自 contract_daily（逐合约日线）
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='contract_daily') THEN
        UPDATE dim_contract dc
           SET list_date        = s.first_td,
               first_trade_date = s.first_td,
               last_trade_date  = s.last_td,
               updated_at       = now()
          FROM (
              SELECT symbol AS cd_symbol,
                     min(trade_date) AS first_td,
                     max(trade_date) AS last_td
              FROM contract_daily
              GROUP BY symbol
          ) s
         WHERE dc.contract_code = s.cd_symbol
           AND (dc.list_date IS NULL OR dc.last_trade_date IS NULL);
        RAISE NOTICE 'dim_contract 生命周期日期已从 contract_daily 回填';
    END IF;
END $$;

-- 3.4 回填 is_main：出现在 main_contract_map 最新一日 underlying 的合约视为当前主力
DO $$
DECLARE
    v_latest date;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='main_contract_map') THEN
        SELECT max(trade_date) INTO v_latest FROM main_contract_map;
        UPDATE dim_contract dc
           SET is_main   = true,
               updated_at = now()
         WHERE dc.contract_code IN (
                 SELECT DISTINCT underlying
                 FROM main_contract_map m
                 WHERE m.trade_date = v_latest
                   AND m.underlying ~ '^[A-Za-z]+\d{4}$');
        RAISE NOTICE 'dim_contract.is_main 已按 main_contract_map 最新日(%)标记主力合约',
                     v_latest;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 4. 便捷视图：品种 → 合约族（上层取数辅助）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_variety_contracts AS
SELECT v.variety_code, v.variety_name, v.exchange, v.main_symbol,
       count(c.contract_code) FILTER (WHERE c.is_active) AS active_contracts,
       min(c.contract_ym) FILTER (WHERE c.is_active)     AS first_ym,
       max(c.contract_ym) FILTER (WHERE c.is_active)     AS last_ym,
       count(*) FILTER (WHERE c.is_main)                 AS main_contracts
FROM dim_variety v
LEFT JOIN dim_contract c ON c.variety_code = v.variety_code
GROUP BY v.variety_code, v.variety_name, v.exchange, v.main_symbol;

COMMENT ON VIEW v_variety_contracts IS
    '品种 → 合约族概览。上层用 variety_code 即可拿到主力符号与在市合约范围。';

COMMIT;
