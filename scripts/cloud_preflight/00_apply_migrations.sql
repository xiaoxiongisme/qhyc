-- 00_apply_migrations.sql · 在 STAGING 克隆库上应用 007/008/009
-- 运行：psql <staging-dsn> -v ON_ERROR_STOP=1 -f 00_apply_migrations.sql
-- 说明：三个迁移各自带 BEGIN/COMMIT 且幂等（ON CONFLICT DO UPDATE/NOTHING），
--       可重复跑。本文件仅做聚合，便于一步应用。
\i ../migrations/007_dim_tables.sql
\i ../migrations/008_cfg_tables.sql
\i ../migrations/009_l0_l1_l2_stored_procs.sql
