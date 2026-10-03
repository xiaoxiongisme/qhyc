-- =============================================================================
-- 015 · 品种别名归并：PTA = TA（同一品种，TA 是交易所代码、PTA 是名称/英文缩写）
-- 日期：2026-10-03　用户澄清：「期货品种TA指的就是精对苯二甲酸(PTA)…一个是代码一个是名称」
-- 适用：本地库与云端库同构执行（幂等，可重复运行）
--
-- 背景
-- ----
-- dim_variety 里同时存在 TA（郑商所 PTA，mult=5/tick=2）与 PTA（无中文名、无乘数）。
-- 二者是**同一品种的两种写法**，不是两个品种（类比 JD ↔ 鸡蛋）。
-- 若不处理：任何以 "PTA" 传入的查询会命中那行空规格，variety_spec() 抛
-- 「multiplier 为空」；且字典表出现同一品种两行、互相矛盾。
--
-- 处理
-- ----
-- 把规范品种 TA 的规格**镜像**到 PTA 行（乘数/最小变动价位/报价单位/交易所），
-- 并在 source 标注 alias_of:TA，使「别名行」可被审计与追溯。
-- 刻意**不**删除 PTA 行：部分数据源确实以 PTA 作代码（郑商所英文代码），
-- 删行会让这些来源查不到品种。
--
-- ⚠ 其余疑似别名（ME/MA、OP/PG、TC/SC 等）**刻意未处理**：
--   ME(甲醇)/OP(液化石油气)/TC(燃料油) 与 MA/PG/SC 是否确为同一品种、
--   还是不同交易所的独立品种，需逐个核实交易所代码后再定，盲目镜像会串品种。
-- =============================================================================

BEGIN;

UPDATE dim_variety alias
   SET variety_name    = canon.variety_name,
       exchange        = canon.exchange,
       tick_size       = canon.tick_size,
       multiplier      = canon.multiplier,
       quote_unit      = canon.quote_unit,
       main_symbol     = canon.main_symbol,
       sector          = COALESCE(alias.sector, canon.sector),
       is_active       = true,
       source          = 'alias_of:' || canon.variety_code,
       updated_at      = now()
  FROM dim_variety canon
 WHERE canon.variety_code = 'TA'
   AND alias.variety_code = 'PTA'
   AND (alias.multiplier IS DISTINCT FROM canon.multiplier
        OR alias.variety_name IS DISTINCT FROM canon.variety_name);

COMMENT ON TABLE dim_variety IS
    '品种字典（数据层唯一权威）。variety_code 为标准大写品种码（TA）。'
    '交易所并存的**同一品种别名**（如 PTA = TA）以 source=alias_of:<规范码> 标注，'
    '规格镜像自规范行；两者都可查，但统计口径应只取 alias_of 为空的规范行。';

COMMIT;