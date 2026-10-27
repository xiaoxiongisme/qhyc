-- =============================================================================
-- migration: 008_fix_b_cs_symbol_mapping
-- 目的: 补全 B(豆二) / CS(玉米淀粉) 两个 DCE 品种在维度表中的缺失条目,
--       使 app.data.* / barstore / level_touch 回测在这两个品种上能正常解析符号。
--
-- 背景:
--   2026-10-24 全市场扫描发现 B、CS 在 main_continuous 有数据, 但:
--     - dim_symbol 缺 namespace='main' 的首选符号 -> variety_spec() 报 "no row"
--     - dim_variety 缺品种规格 -> 后续解析再次失败
--   其余 57 个品种均完整, 仅此 2 个 DCE 品种缺失(疑似 001/007 迁移遗漏)。
--   本迁移以同属 DCE 的 EB(苯乙烯) 行为模板镜像补全, tick/multiplier 按 DCE 规则
--   (豆二/玉米淀粉 均为 10 吨/手, tick=1 元/吨)。
--
-- 幂等性: 两张表当前均无唯一/主键约束(由应用层管理), 故用 NOT EXISTS 守卫,
--         重复执行不会重复插入或报错。
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1) dim_symbol: 补全 B / CS 的 main 命名空间首选连续符号
--    说明: 两表当前无唯一/主键约束(由应用层管理), 故用 NOT EXISTS 守卫保证
--          幂等; 即使未来补了约束, NOT EXISTS 同样安全。
-- ---------------------------------------------------------------------------
INSERT INTO dim_symbol (symbol, product, namespace, exchange, is_preferred, source_tables)
SELECT 'B888',  'B',  'main', '', true, d.source_tables
FROM dim_symbol d
WHERE d.product = 'EB' AND d.namespace = 'main'
  AND NOT EXISTS (SELECT 1 FROM dim_symbol WHERE product='B'  AND namespace='main')
LIMIT 1;

INSERT INTO dim_symbol (symbol, product, namespace, exchange, is_preferred, source_tables)
SELECT 'CS888', 'CS', 'main', '', true, d.source_tables
FROM dim_symbol d
WHERE d.product = 'EB' AND d.namespace = 'main'
  AND NOT EXISTS (SELECT 1 FROM dim_symbol WHERE product='CS' AND namespace='main')
LIMIT 1;

-- ---------------------------------------------------------------------------
-- 2) dim_variety: 补全 B / CS 的品种规格(以 EB 为模板, DCE 规则)
-- ---------------------------------------------------------------------------
INSERT INTO dim_variety
  (variety_code, variety_name, exchange, sector, tick_size, multiplier, quote_unit,
   main_symbol, is_active, source, updated_at, margin_rate_long, margin_rate_short,
   margin_per_lot_long, margin_min_rate, margin_max_rate, exchange_name_src,
   official_name, continuous_follow_rule, is_archive_only)
SELECT 'B', '豆二', 'DCE', '', 1, 10, '元/吨',
       'B888', true, 'migration_008', now(),
       0.13, 0.11, 0, 0.11, 0.11, 'akshare', '豆二', 'NEAR_MONTH', false
WHERE NOT EXISTS (SELECT 1 FROM dim_variety WHERE variety_code = 'B');

INSERT INTO dim_variety
  (variety_code, variety_name, exchange, sector, tick_size, multiplier, quote_unit,
   main_symbol, is_active, source, updated_at, margin_rate_long, margin_rate_short,
   margin_per_lot_long, margin_min_rate, margin_max_rate, exchange_name_src,
   official_name, continuous_follow_rule, is_archive_only)
SELECT 'CS', '玉米淀粉', 'DCE', '', 1, 10, '元/吨',
       'CS888', true, 'migration_008', now(),
       0.13, 0.11, 0, 0.11, 0.11, 'akshare', '玉米淀粉', 'NEAR_MONTH', false
WHERE NOT EXISTS (SELECT 1 FROM dim_variety WHERE variety_code = 'CS');

-- ---------------------------------------------------------------------------
-- 3) 校验: 应各返回 1 行, is_preferred=true
-- ---------------------------------------------------------------------------
-- SELECT product, symbol, is_preferred FROM dim_symbol
--   WHERE product IN ('B','CS') AND namespace='main' ORDER BY product;
-- SELECT variety_code, variety_name, exchange, tick_size, multiplier, main_symbol
--   FROM dim_variety WHERE variety_code IN ('B','CS') ORDER BY variety_code;
