-- 00_apply_migrations.sql · 在 STAGING 克隆库上应用 007/008/009/010/011
-- 运行：psql <staging-dsn> -v ON_ERROR_STOP=1 -f 00_apply_migrations.sql
-- 说明：各迁移自带 BEGIN/COMMIT 且幂等（CREATE OR REPLACE / ON CONFLICT DO NOTHING /
--       只填 NULL 的 UPDATE），可重复跑。本文件仅做聚合，便于一步应用。
-- 顺序不可调换：010 修复 sp_build_l2_roll_segment（CREATE OR REPLACE，覆盖 009 的同名过程），
--               011 回填 dim_variety 的 exchange/main_symbol（补采链路硬依赖）。
\i ../migrations/007_dim_tables.sql
\i ../migrations/008_cfg_tables.sql
\i ../migrations/009_l0_l1_l2_stored_procs.sql
\i ../migrations/010_fix_l2_float_semantics.sql
\i ../migrations/011_backfill_dim_variety_symbols.sql
