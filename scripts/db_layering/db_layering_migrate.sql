-- ============================================================================
-- 期货数据层分层隔离迁移脚本  (qhyc : PostgreSQL 16.4 + TimescaleDB 2.17.1)
-- 作者：数据架构整改  |  版本：v1  |  性质：幂等、可重跑、可回滚
--
-- 目标：把平铺在 public / 全归超级用户 futures 的现状，落地为
--   schema: l0_raw / l1_mkt / l2_adj / l3_ref / app_state / access
--   角色:   etl_ingest / l1_builder / l2_builder / ref_maintainer / app_write / app_read
--   核心闸门：app_read 只能看 access 视图，完全看不到 l0_raw（防误用原始/错误表）
--
-- ⚠ 执行前必做（Phase 0）：
--   1) 维护窗口；2) pg_dump 全库备份；3) 改角色密码（见 Phase 1 注释）
--   4) 先跑 db_layering_verify.sql 看现状；跑完再跑一次看结果
-- 实测：压缩/非压缩超表 ALTER TABLE SET SCHEMA 均直接成功，无需解压。
-- ============================================================================

-- ============================ Phase 1: 角色 ================================
-- 安全提示：以下密码为占位符，上线前务必 ALTER ROLE <r> PASSWORD '<强密码>'
--          并写入 .pgpass / 密钥管理，禁止明文入库。
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='etl_ingest')   THEN CREATE ROLE etl_ingest   LOGIN PASSWORD 'CHANGE_ME_ETL';     END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='l1_builder')   THEN CREATE ROLE l1_builder   LOGIN PASSWORD 'CHANGE_ME_L1';     END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='l2_builder')   THEN CREATE ROLE l2_builder   LOGIN PASSWORD 'CHANGE_ME_L2';     END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='ref_maintainer')THEN CREATE ROLE ref_maintainer LOGIN PASSWORD 'CHANGE_ME_REF';  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app_write')    THEN CREATE ROLE app_write    LOGIN PASSWORD 'CHANGE_ME_WRITE'; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app_read')     THEN CREATE ROLE app_read     LOGIN PASSWORD 'CHANGE_ME_READ';  END IF;
  -- 不授予 SUPERUSER；futures 保留为 DBA。
END$$;

-- ============================ Phase 2: Schema ==============================
CREATE SCHEMA IF NOT EXISTS l0_raw;
CREATE SCHEMA IF NOT EXISTS l1_mkt;
CREATE SCHEMA IF NOT EXISTS l2_adj;
CREATE SCHEMA IF NOT EXISTS l3_ref;
CREATE SCHEMA IF NOT EXISTS app_state;
CREATE SCHEMA IF NOT EXISTS access;

-- ====================== Phase 3: 映射表 + 目录扩展 =========================
-- 持久化映射，供本脚本重跑与 db_layering_rollback.sql 复用（单一真相源）。
CREATE TABLE IF NOT EXISTS public.layer_migration_map (
  tab           text PRIMARY KEY,
  target        text NOT NULL,
  layer         text,
  renamed_from  text
);
TRUNCATE public.layer_migration_map;

-- 回填全量映射（tab=物理表名, target=目标schema, layer=分层, renamed_from=改名前名）
INSERT INTO public.layer_migration_map (tab, target, layer, renamed_from) VALUES
  -- L0 基础数据 (l0_raw)
  ('fut_kline','l0_raw','L0',NULL),
  ('daily_bar','l0_raw','L0',NULL),
  ('minute_bar','l0_raw','L0',NULL),
  ('warehouse_receipt','l0_raw','L0',NULL),
  ('inventory','l0_raw','L0',NULL),
  ('spot_basis','l0_raw','L0',NULL),
  ('roll_yield','l0_raw','L0',NULL),
  ('member_position_rank','l0_raw','L0',NULL),
  ('macro_china','l0_raw','L0',NULL),
  ('contract_daily','l0_raw','L0',NULL),
  ('dim_variety_tick_history','l0_raw','L0',NULL),
  -- hourly_bar：**不再特殊处理**（R1/031 改名后已是普通分层表）。
  -- 原名 hourly_bar_akshare_deprecated 是 G4 隔离时的临时名，但 G9 退役
  -- fut_kline.continuous 后它成为小时线主连唯一存储，"deprecated" 后缀属误导性命名，
  -- 已在 031 改回 hourly_bar；此处按普通表走 4b 通用循环 + Phase 7 通用 shim。
  ('hourly_bar','l0_raw','L0',NULL),
  -- L1 归一化行情 (l1_mkt)
  ('bar_5m','l1_mkt','L1',NULL),
  ('bar_15m','l1_mkt','L1',NULL),
  ('bar_30m','l1_mkt','L1',NULL),
  ('bar_60m','l1_mkt','L1',NULL),
  ('member_position_rank_summary','l1_mkt','L1',NULL),
  -- L2 复权/派生 (l2_adj)
  ('roll_segment','l2_adj','L2',NULL),
  ('main_continuous','l2_adj','L2',NULL),
  ('factor_value','l2_adj','L2',NULL),
  ('factor_ic_roll','l2_adj','L2',NULL),
  ('factor_registry','l2_adj','L2',NULL),
  ('prediction_result','l2_adj','L2',NULL),
  ('model_weights','l2_adj','L2',NULL),
  ('transmission_weights','l2_adj','L2',NULL),
  ('sector_index','l2_adj','L2',NULL),
  -- L3 字典/配置/映射 (l3_ref)
  ('dim_contract','l3_ref','L3',NULL),
  ('dim_exchange','l3_ref','L3',NULL),
  ('dim_main_contract_inferred','l3_ref','L3',NULL),
  ('dim_symbol','l3_ref','L3',NULL),
  ('dim_trading_cost','l3_ref','L3',NULL),
  ('dim_variety','l3_ref','L3',NULL),
  ('cfg_basic_indicator','l3_ref','L3',NULL),
  ('cfg_feature_switch','l3_ref','L3',NULL),
  ('cfg_holiday','l3_ref','L3',NULL),
  ('cfg_symbol_margin','l3_ref','L3',NULL),
  ('cfg_trading_session','l3_ref','L3',NULL),
  ('contract_code_map','l3_ref','L3',NULL),
  ('main_contract_map','l3_ref','L3',NULL),
  ('trade_calendar','l3_ref','L3',NULL),
  ('data_layer_catalog','l3_ref','L3',NULL),
  -- 运行时状态 (app_state)
  ('fusion_position','app_state','APP',NULL),
  ('fusion_push_log','app_state','APP',NULL),
  ('fusion_signal_log','app_state','APP',NULL),
  ('pipeline_push_log','app_state','APP',NULL),
  ('pipeline_run','app_state','APP',NULL),
  ('sync_state','app_state','APP',NULL),
  ('task_run','app_state','APP',NULL),
  ('briefing_signal','app_state','APP',NULL),
  ('portfolio_equity','app_state','APP',NULL),
  ('anomaly_ticket','app_state','APP',NULL),
  ('backtest_detail','app_state','APP',NULL),
  ('backtest_result','app_state','APP',NULL),
  ('sector_map','app_state','APP',NULL);

-- data_layer_catalog 扩展：加 physical_schema 列 + 唯一索引（让目录成为物理真相源）
-- 按表**实际所在 schema** 判定（首轮可能已迁入 l3_ref，直接写 public. 会报 does not exist）
DO $$
DECLARE sch text;
BEGIN
  SELECT n.nspname INTO sch FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE c.relname='data_layer_catalog' AND c.relkind IN ('r','p') LIMIT 1;
  IF sch IS NOT NULL THEN
    EXECUTE format('ALTER TABLE %I.data_layer_catalog ADD COLUMN IF NOT EXISTS physical_schema text', sch);
    EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS uq_dlc_table ON %I.data_layer_catalog(table_name)', sch);
    RAISE NOTICE 'data_layer_catalog 扩展完成（schema=%）', sch;
  END IF;
END$$;

-- ====================== Phase 4: 迁表（幂等 + 异常捕获） ====================
-- migrate_log 建表（放在最前：后续所有阶段都要写它，且 EXCEPTION 块内 INSERT
-- 失败会把**同块已执行的 DDL 一起回滚**，故必须保证表先存在）。
CREATE TABLE IF NOT EXISTS public.migrate_log (ts timestamptz DEFAULT now(), tab text, action text, detail text);

-- 4a 已删除（原 hourly_bar 改名隔离块）。
-- 沿革：原实现把 public.hourly_bar 表改名隔离为 hourly_bar_akshare_deprecated 再迁 l0_raw。
--   ① 2026-10-05 修过它的幂等破口：存在性判断未过滤 relkind，二次执行时
--      public.hourly_bar 已是 VIEW → ALTER TABLE 抛 42809；且该块无 EXCEPTION 兜底，
--      会中断整个脚本（Phase 4b/5/6/7 全不执行），是回滚路径的真实断点。
--   ② 2026-10-06 (R1/031) 该表已改回 hourly_bar，与其他分层表**完全同构**，
--      由下方 4b 通用循环（按 layer_migration_map, renamed_from IS NULL）统一处理，
--      特殊分支随之删除。隔离语义已不存在，无需保留。

-- 4b. 通用迁表循环：仅当表仍在 public 时迁移；压缩超表直接 SET SCHEMA（已实测可行）
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT * FROM public.layer_migration_map WHERE renamed_from IS NULL LOOP
    BEGIN
      -- data_layer_catalog 最后单独迁，保证循环中回写目录时它仍在 public
      IF r.tab='data_layer_catalog' THEN CONTINUE; END IF;
      IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                 WHERE n.nspname='public' AND c.relname=r.tab AND c.relkind IN ('r','p')) THEN
        EXECUTE format('ALTER TABLE public.%I SET SCHEMA %I', r.tab, r.target);
        INSERT INTO public.migrate_log(tab,action,detail) VALUES (r.tab,'MOVE', r.target);
        -- 回写目录 physical_schema（不破坏已有 layer/source_layer/derived_by/mutable_policy）
        INSERT INTO public.data_layer_catalog(table_name, layer, physical_schema, updated_at)
          VALUES (r.tab, r.layer, r.target, now())
          ON CONFLICT (table_name) DO UPDATE SET physical_schema=EXCLUDED.physical_schema, updated_at=now();
      ELSE
        INSERT INTO public.migrate_log(tab,action,detail) VALUES (r.tab,'SKIP', 'not in public / already moved');
      END IF;
    EXCEPTION WHEN others THEN
      INSERT INTO public.migrate_log(tab,action,detail) VALUES (r.tab,'ERROR', SQLERRM);
    END;
  END LOOP;
  -- data_layer_catalog 自身最后迁移（循环里它还在 public 时才能被 upsert）
  IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname='public' AND c.relname='data_layer_catalog' AND c.relkind IN ('r','p')) THEN
    EXECUTE 'ALTER TABLE public.data_layer_catalog SET SCHEMA l3_ref';
    INSERT INTO public.migrate_log(tab,action,detail) VALUES ('data_layer_catalog','MOVE','l3_ref');
    INSERT INTO l3_ref.data_layer_catalog(table_name, layer, physical_schema, updated_at)
      VALUES ('data_layer_catalog','L3','l3_ref', now())
      ON CONFLICT (table_name) DO UPDATE SET physical_schema='l3_ref', updated_at=now();
  END IF;
END$$;

-- ============================ Phase 5: 授权 ================================
-- 5.1 schema USAGE
GRANT USAGE ON SCHEMA l0_raw,l1_mkt,l2_adj,l3_ref,app_state,access TO etl_ingest,l1_builder,l2_builder,ref_maintainer,app_write,app_read;

-- 5.2 各角色最小权限（按 matrix；只给 DML，不给 DDL/DROP）
-- etl_ingest: 仅写 l0_raw
GRANT INSERT,UPDATE,DELETE,SELECT            ON ALL TABLES IN SCHEMA l0_raw TO etl_ingest;
GRANT SELECT                                 ON ALL TABLES IN SCHEMA l3_ref TO etl_ingest; -- 联表用
ALTER DEFAULT PRIVILEGES IN SCHEMA l0_raw    GRANT INSERT,UPDATE,DELETE,SELECT ON TABLES TO etl_ingest;

-- l1_builder: 读 l0_raw / 全权 l1_mkt / 读 l3_ref
GRANT SELECT                                 ON ALL TABLES IN SCHEMA l0_raw TO l1_builder;
GRANT INSERT,UPDATE,DELETE,SELECT,TRUNCATE   ON ALL TABLES IN SCHEMA l1_mkt TO l1_builder;
GRANT SELECT                                 ON ALL TABLES IN SCHEMA l3_ref TO l1_builder;
ALTER DEFAULT PRIVILEGES IN SCHEMA l1_mkt    GRANT INSERT,UPDATE,DELETE,SELECT ON TABLES TO l1_builder;

-- l2_builder: 读 l1_mkt / 全权 l2_adj / 读 l3_ref
GRANT SELECT                                 ON ALL TABLES IN SCHEMA l1_mkt TO l2_builder;
GRANT INSERT,UPDATE,DELETE,SELECT,TRUNCATE   ON ALL TABLES IN SCHEMA l2_adj TO l2_builder;
GRANT SELECT                                 ON ALL TABLES IN SCHEMA l3_ref TO l2_builder;
ALTER DEFAULT PRIVILEGES IN SCHEMA l2_adj    GRANT INSERT,UPDATE,DELETE,SELECT ON TABLES TO l2_builder;

-- ref_maintainer: 仅全权 l3_ref
GRANT INSERT,UPDATE,DELETE,SELECT,TRUNCATE   ON ALL TABLES IN SCHEMA l3_ref TO ref_maintainer;
ALTER DEFAULT PRIVILEGES IN SCHEMA l3_ref    GRANT INSERT,UPDATE,DELETE,SELECT ON TABLES TO ref_maintainer;

-- app_write: 仅写 app_state
GRANT INSERT,UPDATE,DELETE,SELECT            ON ALL TABLES IN SCHEMA app_state TO app_write;
ALTER DEFAULT PRIVILEGES IN SCHEMA app_state GRANT INSERT,UPDATE,DELETE,SELECT ON TABLES TO app_write;

-- app_read: 仅可读 access 视图（核心闸门：绝不授予 l0_raw/l1_mkt/l2_adj/l3_ref/app_state）
GRANT SELECT                                 ON ALL TABLES IN SCHEMA access TO app_read;
ALTER DEFAULT PRIVILEGES IN SCHEMA access    GRANT SELECT ON TABLES TO app_read;

-- ====================== Phase 6: access 视图契约 ===========================
-- 这是唯一对外契约；每个名字精确对应一个语义，无歧义。
-- 防错核心：app_read 看不到 l0_raw，access 也没有指向 hourly_bar 的视图。
CREATE OR REPLACE VIEW access.v_bar_60m_main AS
  SELECT * FROM l1_mkt.bar_60m;                       -- 小时线主连(原始/执行空间),2015+
CREATE OR REPLACE VIEW access.v_hourly_main AS
  SELECT * FROM l1_mkt.bar_60m;                        -- 同上别名：bar_60m 即小时线
-- v_fut_kline_cont_hourly / cont_daily 已随 034(G9-a) 退役并删除：
-- 它们读 kind='continuous'，而该 kind 已于 2026-10-04 G9 退役、034 中数据被删除；
-- 小时线主连信号空间改读 l0_raw.hourly_bar（见 app/data/caliber.py ROUTES）。
-- 此处**不再创建**这两个视图，否则会得到空视图（verify 脚本亦已移除其非空断言）。
CREATE OR REPLACE VIEW access.v_fut_kline_raw_hourly AS
  SELECT * FROM l0_raw.fut_kline
  WHERE kind='contract' AND freq IN ('hourly','min60');    -- 小时线原始合约(执行精确价)
CREATE OR REPLACE VIEW access.v_daily_main AS
  SELECT * FROM l0_raw.daily_bar;                      -- 日线主连
CREATE OR REPLACE VIEW access.v_main_continuous AS
  SELECT * FROM l2_adj.main_continuous;                -- 主连连续序列
CREATE OR REPLACE VIEW access.v_dim_trading_cost AS SELECT * FROM l3_ref.dim_trading_cost;
CREATE OR REPLACE VIEW access.v_dim_symbol AS        SELECT * FROM l3_ref.dim_symbol;
CREATE OR REPLACE VIEW access.v_dim_variety AS       SELECT * FROM l3_ref.dim_variety;
CREATE OR REPLACE VIEW access.v_dim_contract AS      SELECT * FROM l3_ref.dim_contract;
CREATE OR REPLACE VIEW access.v_main_contract_map AS SELECT * FROM l3_ref.main_contract_map;
CREATE OR REPLACE VIEW access.v_trade_calendar AS    SELECT * FROM l3_ref.trade_calendar;
CREATE OR REPLACE VIEW access.v_roll_segment AS      SELECT * FROM l2_adj.roll_segment;
CREATE OR REPLACE VIEW access.v_sector_index AS      SELECT * FROM l2_adj.sector_index;
CREATE OR REPLACE VIEW access.v_cfg_trading_session AS SELECT * FROM l3_ref.cfg_trading_session;

-- app_read 对 access 视图的最终 SELECT 授权（视图在 access 下，已建，需显式授予）
GRANT SELECT ON ALL TABLES IN SCHEMA access TO app_read;

-- ====================== Phase 7: public 兼容 shim ==========================
-- 让现有 SQLAlchemy 模型(schema=None->public)零改动继续跑；长期应改连 access 后删除。
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT * FROM public.layer_migration_map WHERE renamed_from IS NULL LOOP
    BEGIN
      EXECUTE format('CREATE OR REPLACE VIEW public.%I AS SELECT * FROM %I.%I',
                     r.tab, r.target, r.tab);
    EXCEPTION WHEN others THEN
      INSERT INTO public.migrate_log(tab,action,detail) VALUES (r.tab,'SHIM_ERR', SQLERRM);
    END;
  END LOOP;
  -- hourly_bar 兼容 shim：指向被隔离的原始表（行为不变），强制新代码改用 access.v_bar_60m_main
  BEGIN
    -- R1/031 改名后指向 l0_raw.hourly_bar（原名 hourly_bar_akshare_deprecated 已废弃）
    EXECUTE 'CREATE OR REPLACE VIEW public.hourly_bar AS SELECT * FROM l0_raw.hourly_bar';
  EXCEPTION WHEN others THEN
    INSERT INTO public.migrate_log(tab,action,detail) VALUES ('hourly_bar','SHIM_ERR', SQLERRM);
  END;
END$$;

-- ============================ Phase 8: 校验摘要 ============================
SELECT '=== 迁移结果摘要 ===' AS msg;
SELECT tab, action, detail FROM public.migrate_log ORDER BY ts, tab;
SELECT '=== 各层表计数（应无表残留在 public，除 shim/schema_migrations）===' AS msg;
SELECT n.nspname AS schema, count(*) AS tables
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE c.relkind IN ('r','p') AND n.nspname NOT LIKE 'pg_%' AND n.nspname NOT LIKE '_timescaledb%'
GROUP BY 1 ORDER BY 1;
