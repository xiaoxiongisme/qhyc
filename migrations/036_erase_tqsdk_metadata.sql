-- =============================================================================
-- 036 · 擦除 DB 元数据中的天勤（tqsdk）字面残留
-- 日期：2026-10-06（C9 去天勤收尾；死资产审计发现 DB 侧漏擦）
-- =============================================================================
--
-- 背景
-- ----
-- C9 去天勤专项已把仓库侧（requirements.txt / .env.example / docs / migrations /
-- app / scripts）的 tqsdk|TqApi|TQSDK 全部清零，但**数据库元数据**漏了：
-- 死资产审计发现治理登记表与表注释里仍有"天勤"字面。
--
-- 本迁移处理（逐条核对后的精确名单）
-- ----------------------------------
-- 1) l3_ref.data_layer_catalog.purpose   fut_kline = '天勤全频原始行情(freq×kind)'
--    → 改为中性描述（该表现在只剩 contract 真实合约行情，见 034）
-- 2) l3_ref.dim_symbol 表注释            '… tqsdk=KQ.m@天勤原生码 …'
--    → 029 已把 namespace 取值从 'tqsdk' 改为 'kq'，注释需同步为 kq
--
-- 说明
-- ----
-- * 全库 schema 级自查已确认：列名含 tqsdk = 0、函数定义含 tqsdk = 0、
--   视图定义含 tqsdk = 0（列名/对象名层面早已清零，本迁移只处理注释文本）。
-- * 只改注释与元数据，**不动任何数据、不改任何列名**。
-- * 幂等：用 WHERE ... ILIKE '%天勤%' 守卫，已擦除则 0 行。
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.migrate_log (ts timestamptz DEFAULT now(), tab text, action text, detail text);

-- -----------------------------------------------------------------------------
-- 1) 治理登记表：fut_kline 的 purpose 去天勤化
--    034 后该表只剩 kind='contract'（真实合约行情），描述一并更正
-- -----------------------------------------------------------------------------
UPDATE l3_ref.data_layer_catalog
   SET purpose = '全频原始行情(freq×kind)；034 后仅保留 kind=contract 真实合约行情'
 WHERE table_name = 'fut_kline'
   AND (purpose ILIKE '%天勤%' OR purpose ILIKE '%tqsdk%');

-- 兜底：catalog 其它行若也有天勤字面，统一替换
UPDATE l3_ref.data_layer_catalog
   SET purpose = replace(replace(purpose, '天勤', ''), 'tqsdk', '')
 WHERE purpose ILIKE '%天勤%' OR purpose ILIKE '%tqsdk%';

-- -----------------------------------------------------------------------------
-- 2) dim_symbol 表注释：tqsdk=KQ.m@天勤原生码 → kq=KQ.m@原生码
--    （029 已将 namespace 取值 'tqsdk' 改为 'kq'，注释需与实现一致）
-- -----------------------------------------------------------------------------
DO $$
DECLARE nsp name := 'l3_ref'; tbl name := 'dim_symbol'; old_cmt text;
BEGIN
  SELECT obj_description(c.oid,'pg_class') INTO old_cmt
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname=nsp AND c.relname=tbl;

  IF old_cmt ILIKE '%天勤%' OR old_cmt ILIKE '%tqsdk%' THEN
    EXECUTE format(
      'COMMENT ON TABLE %I.%I IS %L', nsp, tbl,
      replace(replace(old_cmt, 'tqsdk=KQ.m@天勤原生码', 'kq=KQ.m@原生码'),
              ' / tqsdk=', ' / kq='));
    INSERT INTO public.migrate_log(tab,action,detail)
      VALUES ('dim_symbol','ERASE_TQSDK','表注释去天勤字面前置完成');
  ELSE
    INSERT INTO public.migrate_log(tab,action,detail)
      VALUES ('dim_symbol','SKIP','注释无天勤字面或表不存在');
  END IF;
END $$;

-- -----------------------------------------------------------------------------
-- 3) 兜底：扫描并擦除**全部**用户对象注释中的天勤字面（表级 + 列级）
-- -----------------------------------------------------------------------------
DO $$
DECLARE r record; new_cmt text;
BEGIN
  -- 表注释
  FOR r IN SELECT n.nspname AS ns, c.relname AS tbl, obj_description(c.oid,'pg_class') AS cmt
           FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
           WHERE n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
             AND n.nspname NOT LIKE 'timescaledb%' AND n.nspname NOT LIKE '\_timescaledb%'
             AND c.relkind IN ('r','p','v','m')
             AND obj_description(c.oid,'pg_class') ILIKE '%天勤%'
  LOOP
    new_cmt := replace(replace(r.cmt, '天勤', ''), 'tqsdk', 'kq');
    EXECUTE format('COMMENT ON TABLE %I.%I IS %L', r.ns, r.tbl, new_cmt);
    INSERT INTO public.migrate_log(tab,action,detail)
      VALUES (r.ns||'.'||r.tbl,'ERASE_TQSDK','表注释（兜底扫描）');
  END LOOP;

  -- 列注释
  FOR r IN SELECT n.nspname AS ns, c.relname AS tbl, a.attname AS col,
                  col_description(c.oid, a.attnum) AS cmt
           FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
           JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
           WHERE n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
             AND n.nspname NOT LIKE 'timescaledb%' AND n.nspname NOT LIKE '\_timescaledb%'
             AND c.relkind IN ('r','p','v','m')
             AND col_description(c.oid, a.attnum) ILIKE '%天勤%'
  LOOP
    new_cmt := replace(replace(r.cmt, '天勤', ''), 'tqsdk', 'kq');
    EXECUTE format('COMMENT ON COLUMN %I.%I.%I IS %L', r.ns, r.tbl, r.col, new_cmt);
    INSERT INTO public.migrate_log(tab,action,detail)
      VALUES (r.ns||'.'||r.tbl||'.'||r.col,'ERASE_TQSDK','列注释（兜底扫描）');
  END LOOP;
END $$;

COMMIT;

-- =============================================================================
-- 验收 SQL（应用后逐条执行，括号内为期望 0）
-- =============================================================================
-- SELECT count(*) FROM l3_ref.data_layer_catalog
--   WHERE purpose ILIKE '%天勤%' OR purpose ILIKE '%tqsdk%';              -- 0
-- SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
--   WHERE n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
--     AND obj_description(c.oid,'pg_class') ILIKE '%天勤%';                 -- 0
-- SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
--   JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
--   WHERE n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
--     AND col_description(c.oid,a.attnum) ILIKE '%天勤%';                   -- 0
-- SELECT purpose FROM l3_ref.data_layer_catalog WHERE table_name='fut_kline';
-- SELECT obj_description('l3_ref.dim_symbol'::regclass,'pg_class');
