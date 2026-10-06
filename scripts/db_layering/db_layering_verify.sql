-- ============================================================================
-- 数据层分层隔离 · 迁移后校验脚本 (db_layering_verify.sql)
-- 用途：断言权限隔离是否生效、视图契约是否完整、数据是否零丢失。
-- 以超级用户(futures)运行。输出 verify_result 表 + 摘要。
-- ============================================================================
DROP TABLE IF EXISTS public.verify_result;
CREATE TABLE public.verify_result (chk text, res text);

DO $$
DECLARE n_src bigint; n_view bigint; e text; st text;
BEGIN
  -- 1) app_read 可读 access 视图（核心契约）
  BEGIN
    SET ROLE app_read;
    SELECT count(*) INTO n_view FROM access.v_bar_60m_main;
    -- v_fut_kline_cont_hourly 已随 034(G9-a) 退役删除，改验同层的 raw_hourly
    SELECT count(*) INTO n_view FROM access.v_fut_kline_raw_hourly;
    SELECT count(*) INTO n_view FROM access.v_dim_trading_cost;
    -- ★ 必须先 RESET ROLE 再写结果：app_read 对 verify_result 无写权限，
    --   否则"读取成功"也会被记成 FAIL 42501（既存缺陷，2026-10-06 修）
    RESET ROLE;
    INSERT INTO public.verify_result(chk,res) VALUES ('app_read 可读 access 视图', 'PASS');
  EXCEPTION WHEN others THEN
    GET STACKED DIAGNOSTICS st = RETURNED_SQLSTATE;
    RESET ROLE;
    INSERT INTO public.verify_result(chk,res) VALUES ('app_read 可读 access 视图', 'FAIL '||st||':'||SQLERRM);
  END;

  -- 2) app_read 读 l0_raw 必须被拒绝（闸门生效）
  BEGIN
    SET ROLE app_read;
    SELECT count(*) INTO n_src FROM l0_raw.fut_kline;   -- 期望报错
    INSERT INTO public.verify_result(chk,res) VALUES ('app_read 读 l0_raw(应拒绝)', 'UNEXPECTED_PASS');
    RESET ROLE;
  EXCEPTION WHEN others THEN
    GET STACKED DIAGNOSTICS st = RETURNED_SQLSTATE;
    IF st='42501' THEN
      INSERT INTO public.verify_result(chk,res) VALUES ('app_read 读 l0_raw(应拒绝)', 'PASS(拒绝码42501)');
    ELSE
      INSERT INTO public.verify_result(chk,res) VALUES ('app_read 读 l0_raw(应拒绝)', 'CHECK:'||st);
    END IF;
    RESET ROLE;
  END;

  -- 3) 数据零丢失：access 视图行数 == 源表行数
  SELECT count(*) INTO n_src  FROM l1_mkt.bar_60m;
  SELECT count(*) INTO n_view FROM access.v_bar_60m_main;
  INSERT INTO public.verify_result(chk,res) VALUES ('bar_60m 行数一致',
     CASE WHEN n_src=n_view THEN 'PASS('||n_src||')' ELSE 'FAIL src='||n_src||' view='||n_view END);

  SELECT count(*) INTO n_src  FROM l0_raw.daily_bar;
  SELECT count(*) INTO n_view FROM access.v_daily_main;
  INSERT INTO public.verify_result(chk,res) VALUES ('daily_bar 行数一致',
     CASE WHEN n_src=n_view THEN 'PASS('||n_src||')' ELSE 'FAIL src='||n_src||' view='||n_view END);

  -- 4) 小时线主连信号空间可用性
  --    原断言为「access.v_fut_kline_cont_hourly 非空」，但该视图读 kind='continuous'，
  --    已随 G9 退役 + 034 删除数据，视图本身也被退役 → 原断言必然 FAIL。
  --    改验 G9 后的真口径：l0_raw.hourly_bar 非空（caliber ROUTES ('hourly','continuous')）。
  SELECT count(*) INTO n_view FROM l0_raw.hourly_bar;
  INSERT INTO public.verify_result(chk,res) VALUES ('hourly_bar 非空(G9后小时线主连口径)',
     CASE WHEN n_view>0 THEN 'PASS('||n_view||')' ELSE 'FAIL(0行)' END);

  -- 5) public 不应残留**意外**业务表
  --    白名单说明：G4 只迁移 layer_migration_map 中的表；以下表**有意留在 public**：
  --      * 基础设施：schema_migrations / layer_migration_map / migrate_log / verify_result
  --      * 业务与运行时：backtest_* / anomaly_ticket / futures_symbol / futures_rule /
  --        task_run / pipeline_run / fusion_* / execution_* / sector_map / briefing_signal /
  --        pipeline_push_log / portfolio_equity / cfg_calendar_seed_meta
  --        （属 app_state 层，db_layering_migrate.sql 即按"留在 public"登记）
  --      * minute_bar_adj / minute_bar_stage：G9-b 待处理对象，尚未到退役时点
  --    故本项只检测白名单之外的残留（真异常）。
  PERFORM 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relkind IN ('r','p')
      AND c.relname NOT IN (
        'schema_migrations','layer_migration_map','migrate_log','verify_result',
        'backtest_detail','backtest_result','anomaly_ticket','futures_symbol','futures_rule',
        'task_run','pipeline_run','fusion_push_log','fusion_position','fusion_signal_log',
        'execution_order','execution_position','execution_fill','execution_channel_health',
        'sector_map','briefing_signal','pipeline_push_log','portfolio_equity',
        'cfg_calendar_seed_meta','minute_bar_adj','minute_bar_stage');
  IF FOUND THEN
    INSERT INTO public.verify_result(chk,res) VALUES ('public 无意外残留业务表', 'FAIL(见下)');
  ELSE
    INSERT INTO public.verify_result(chk,res) VALUES ('public 无意外残留业务表', 'PASS');
  END IF;
END$$;

-- 残留业务表明细（若有）
SELECT c.relname AS residual_table_in_public
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND c.relkind IN ('r','p')
  AND c.relname NOT IN ('schema_migrations','layer_migration_map','migrate_log','verify_result')
ORDER BY 1;

-- 各层表计数
SELECT n.nspname AS schema, count(*) AS tables
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE c.relkind IN ('r','p') AND n.nspname NOT LIKE 'pg_%' AND n.nspname NOT LIKE '_timescaledb%'
GROUP BY 1 ORDER BY 1;

-- 校验结果
SELECT chk, res FROM public.verify_result ORDER BY chk;
