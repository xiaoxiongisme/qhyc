-- 045_restore_uq_dim_trading_cost.sql
-- 恢复 dim_trading_cost 的业务唯一约束（被 db_layering 迁移降级为普通唯一索引）
--
-- 【问题】
--   migrations/013_trading_cost_dict.sql 第 60 行把业务唯一键定义为
--     CONSTRAINT uq_dim_trading_cost UNIQUE (variety_code, instrument_kind, action, scope_kind, effective_from)
--   但 db_layering（scripts/db_layering/db_layering_migrate.sql）把该表迁到 l3_ref schema 后，
--   重建出来的是 `CREATE UNIQUE INDEX uq_dim_trading_cost`，**不是约束**。
--
--   PostgreSQL 的 `ON CONFLICT ON CONSTRAINT <name>` 只认 pg_constraint 里的条目，
--   普通唯一索引不匹配 → 任何用该写法的脚本一执行就抛
--     psycopg.errors.UndefinedObject: constraint "uq_dim_trading_cost" for table "dim_trading_cost" does not exist
--
-- 【影响面：3 个脚本全部不可用（且是静默的）】
--   scripts/load_cost_dict.py
--   scripts/rebuild_dict_from_akshare.py
--   scripts/sync_cost_from_exchange.py
--   → 这正是 dim_trading_cost 停在 2026-10-08 15:05 的**真根因**：
--     不只是"没调度"，而是即使人工去跑也会崩。
--
-- 【修法】
--   用 `ADD CONSTRAINT ... USING INDEX` 把现有唯一索引就地提升为约束：
--   不重建数据、不锁表重写，元数据级操作，秒级完成。
--
-- 【幂等】
--   先查 pg_constraint，已存在则跳过。

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM pg_constraint c
          JOIN pg_class     t ON t.oid = c.conrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
         WHERE c.conname = 'uq_dim_trading_cost'
           AND t.relname = 'dim_trading_cost'
           AND c.contype = 'u'
    ) THEN
        RAISE NOTICE '[045] uq_dim_trading_cost 约束已存在，跳过';
        RETURN;
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM pg_class     t
          JOIN pg_namespace n ON n.oid = t.relnamespace
         WHERE t.relname = 'dim_trading_cost'
           AND t.relkind = 'r'
    ) THEN
        RAISE EXCEPTION '[045] 找不到表 dim_trading_cost，无法恢复约束';
    END IF;

    -- 目标表所在 schema 由目录查出（兼容 public 与 l3_ref 两种部署状态）
    EXECUTE format(
        'ALTER TABLE %I.dim_trading_cost ADD CONSTRAINT uq_dim_trading_cost UNIQUE USING INDEX uq_dim_trading_cost',
        (SELECT n.nspname
           FROM pg_class t JOIN pg_namespace n ON n.oid = t.relnamespace
          WHERE t.relname = 'dim_trading_cost' AND t.relkind = 'r')
    );
    RAISE NOTICE '[045] uq_dim_trading_cost 已由唯一索引提升为唯一约束';
END $$;
