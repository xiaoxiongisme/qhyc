-- =============================================================================
-- 032 · R1 改名：l0_raw.hourly_bar_akshare_deprecated → l0_raw.hourly_bar
--                 并修正 data_layer_catalog.physical_schema 空值
-- 日期：2026-10-06（用户裁定 R1）
-- =============================================================================
--
-- 为什么要改
-- ----------
-- 该表名带 `_akshare_deprecated` 后缀，是 G4 分层迁移时为"隔离"而临时改的名
-- （Phase 4a：public.hourly_bar 表 → 改名 → 迁 l0_raw → 再用同名 VIEW 顶替）。
-- 但 G9 退役 fut_kline.continuous 后，它**成了小时线主连的唯一真实存储**：
--   * public.hourly_bar（兼容视图）指向它，app/ 有 43 处引用
--   * CAGG public.hourly_1d_rollup 聚合它
--   * app/data/caliber.py ROUTES 里 ("hourly","continuous") 明确改读 hourly_bar
-- 名字里的 "deprecated" 与实际"唯一活表"严重背离，是**误导性命名**：
-- 死资产审计中差点据此判为废弃表删除 —— 删表将直接断掉小时线主连。
--
-- 改名后它与其他分层表**完全一致**：l0_raw.hourly_bar 表 + public.hourly_bar 视图，
-- 不再需要"隔离改名"的特殊逻辑。故 Phase 4a 的隔离步骤相应简化（见本迁移第 4 步）。
--
-- 幂等性
-- ------
-- 全程用 IF EXISTS / IF NOT EXISTS 守卫，可重复执行；已改名则跳过。
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- 1) 改表名（仅当旧名存在且新名不存在时执行）
--    Postgres 按 OID 追踪依赖，故 public.hourly_bar 视图与 CAGG
--    public.hourly_1d_rollup 会**自动跟随**，无需重建。
-- -----------------------------------------------------------------------------
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname='l0_raw' AND c.relname='hourly_bar_akshare_deprecated'
               AND c.relkind IN ('r','p'))
     AND NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                     WHERE n.nspname='l0_raw' AND c.relname='hourly_bar') THEN
    ALTER TABLE l0_raw.hourly_bar_akshare_deprecated RENAME TO hourly_bar;
    RAISE NOTICE 'R1: l0_raw.hourly_bar_akshare_deprecated -> l0_raw.hourly_bar';
  ELSE
    RAISE NOTICE 'R1: 已改名或对象不存在，跳过';
  END IF;
END $$;

-- -----------------------------------------------------------------------------
-- 2) layer_migration_map：改为普通分层表（renamed_from 置 NULL）
--    置 NULL 后，通用迁表循环（Phase 4b）与通用 shim（Phase 7）会像对待
--    其他表一样处理它，不再走"隔离改名"的特殊分支。
-- -----------------------------------------------------------------------------
UPDATE public.layer_migration_map
   SET tab          = 'hourly_bar',
       target       = 'l0_raw',
       renamed_from = NULL
 WHERE tab = 'hourly_bar_akshare_deprecated';

-- 幂等：若上一步未命中（已改过），补一条标准映射
INSERT INTO public.layer_migration_map(tab, target, layer, renamed_from)
SELECT 'hourly_bar', 'l0_raw', 'L0', NULL
 WHERE NOT EXISTS (SELECT 1 FROM public.layer_migration_map WHERE tab='hourly_bar');

-- -----------------------------------------------------------------------------
-- 3) data_layer_catalog：补齐空着的 physical_schema
--    （审计发现该行为空，导致"该表落在哪个 schema"在治理登记表里查不到）
-- -----------------------------------------------------------------------------
UPDATE l3_ref.data_layer_catalog
   SET table_name      = 'hourly_bar',
       physical_schema = 'l0_raw',
       updated_at      = now()
 WHERE table_name IN ('hourly_bar', 'hourly_bar_akshare_deprecated');

-- 索引名跟随（可选，保持命名一致）
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname='l0_raw' AND c.relname='hourly_bar_akshare_deprecated_pkey') THEN
    ALTER INDEX l0_raw.hourly_bar_akshare_deprecated_pkey RENAME TO hourly_bar_pkey;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname='l0_raw' AND c.relname='hourly_bar_akshare_deprecated_trade_datetime_idx') THEN
    ALTER INDEX l0_raw.hourly_bar_akshare_deprecated_trade_datetime_idx
      RENAME TO hourly_bar_trade_datetime_idx;
  END IF;
END $$;

-- -----------------------------------------------------------------------------
-- 4) 同步更新分层迁移脚本：移除"隔离改名"特殊分支
--    （本迁移不改文件，此处仅留待办说明；文件改动见 commit）
--    scripts/db_layering/db_layering_migrate.sql：
--      * :63  映射元组 ('hourly_bar_akshare_deprecated','l0_raw','L0','hourly_bar')
--             → 删除整个元组（改名后 hourly_bar 走通用循环）
--      * :144-148 Phase 4a 隔离块 → 删除（不再需要隔离改名）
--      * :275  shim 视图定义 → 改为 FROM l0_raw.hourly_bar
-- -----------------------------------------------------------------------------

COMMIT;

-- =============================================================================
-- 验收 SQL（应用后逐条执行，括号内为期望结果）
-- =============================================================================
-- SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
--   WHERE n.nspname='l0_raw' AND c.relname='hourly_bar';                  -- 1（表）
-- SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
--   WHERE n.nspname='l0_raw' AND c.relname='hourly_bar_akshare_deprecated';-- 0（旧名消失）
-- SELECT c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
--   WHERE n.nspname='public' AND c.relname='hourly_bar';                   -- 'v'（视图仍在）
-- SELECT count(*) FROM public.hourly_bar;                                   -- >0（视图仍可出数）
-- SELECT count(*) FROM public.hourly_1d_rollup;                             -- >0（CAGG 仍在）
-- SELECT table_name, physical_schema FROM l3_ref.data_layer_catalog
--   WHERE table_name='hourly_bar';                                          -- hourly_bar | l0_raw
-- SELECT tab, renamed_from FROM public.layer_migration_map
--   WHERE tab='hourly_bar';                                                 -- hourly_bar | (NULL)
