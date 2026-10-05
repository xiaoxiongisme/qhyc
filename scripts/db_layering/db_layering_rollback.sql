-- ============================================================================
-- 数据层分层隔离 · 回滚脚本 (db_layering_rollback.sql)
-- 用途：撤销 db_layering_migrate.sql 的全部改动，回到全 public 平铺状态。
-- 依赖 migrate 阶段持久化的 public.layer_migration_map。
-- 以超级用户(futures)运行；幂等；先删视图/shim，再迁表回 public，最后删角色/schema。
-- ============================================================================

-- 1) 删除 access 视图契约
DROP SCHEMA IF EXISTS access CASCADE;

-- 2) 删除 public 兼容 shim 视图（保留 layer_migration_map / migrate_log / schema_migrations）
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT * FROM public.layer_migration_map WHERE renamed_from IS NULL LOOP
    BEGIN
      EXECUTE format('DROP VIEW IF EXISTS public.%I', r.tab);
    EXCEPTION WHEN others THEN
      -- 忽略已不存在
      NULL;
    END;
  END LOOP;
  -- hourly_bar 兼容 shim
  BEGIN EXECUTE 'DROP VIEW IF EXISTS public.hourly_bar'; EXCEPTION WHEN others THEN NULL; END;
END$$;

-- 3) 把表迁回 public（读取映射；renamed_from 需先改回原名）
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT * FROM public.layer_migration_map LOOP
    BEGIN
      IF r.renamed_from IS NOT NULL THEN
        -- 先改回原名，再迁回 public
        IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                   WHERE n.nspname=r.target AND c.relname=r.tab) THEN
          EXECUTE format('ALTER TABLE %I.%I RENAME TO %I', r.target, r.tab, r.renamed_from);
          EXECUTE format('ALTER TABLE %I.%I SET SCHEMA public', r.target, r.renamed_from);
        END IF;
      ELSE
        IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                   WHERE n.nspname=r.target AND c.relname=r.tab) THEN
          EXECUTE format('ALTER TABLE %I.%I SET SCHEMA public', r.target, r.tab);
        END IF;
      END IF;
    EXCEPTION WHEN others THEN
      INSERT INTO public.migrate_log(tab,action,detail) VALUES (r.tab,'ROLLBACK_ERR', SQLERRM);
    END;
  END LOOP;
END$$;

-- 4) 还原 data_layer_catalog.physical_schema 标记（保留目录，仅清空物理层标记）
UPDATE l3_ref.data_layer_catalog SET physical_schema=NULL WHERE physical_schema IS NOT NULL;

-- 5) 删除角色（revoke 自动随 DROP ROLE 处理）
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app_read')      THEN DROP ROLE app_read;      END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app_write')     THEN DROP ROLE app_write;     END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='ref_maintainer') THEN DROP ROLE ref_maintainer;END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='l2_builder')    THEN DROP ROLE l2_builder;    END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='l1_builder')    THEN DROP ROLE l1_builder;    END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='etl_ingest')    THEN DROP ROLE etl_ingest;    END IF;
END$$;

-- 6) 删除空 schema（表已迁回 public 后应为空）
DROP SCHEMA IF EXISTS l0_raw CASCADE;
DROP SCHEMA IF EXISTS l1_mkt CASCADE;
DROP SCHEMA IF EXISTS l2_adj CASCADE;
DROP SCHEMA IF EXISTS l3_ref CASCADE;
DROP SCHEMA IF EXISTS app_state CASCADE;

-- 7) 清理映射/日志表（如需保留审计可注释此行）
DROP TABLE IF EXISTS public.layer_migration_map;
DROP TABLE IF EXISTS public.migrate_log;

SELECT 'ROLLBACK DONE. 业务表应已全部回到 public.' AS msg;
SELECT n.nspname AS schema, count(*) AS tables
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE c.relkind IN ('r','p') AND n.nspname NOT LIKE 'pg_%' AND n.nspname NOT LIKE '_timescaledb%'
GROUP BY 1 ORDER BY 1;
