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
    SELECT count(*) INTO n_view FROM access.v_fut_kline_cont_hourly;
    SELECT count(*) INTO n_view FROM access.v_dim_trading_cost;
    INSERT INTO public.verify_result(chk,res) VALUES ('app_read 可读 access 视图', 'PASS');
    RESET ROLE;
  EXCEPTION WHEN others THEN
    GET STACKED DIAGNOSTICS st = RETURNED_SQLSTATE;
    INSERT INTO public.verify_result(chk,res) VALUES ('app_read 可读 access 视图', 'FAIL '||st||':'||SQLERRM);
    RESET ROLE;
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

  -- 4) fut_kline 连续小时视图非空（信号空间口径存在）
  SELECT count(*) INTO n_view FROM access.v_fut_kline_cont_hourly;
  INSERT INTO public.verify_result(chk,res) VALUES ('v_fut_kline_cont_hourly 非空',
     CASE WHEN n_view>0 THEN 'PASS('||n_view||')' ELSE 'FAIL(0行)' END);

  -- 5) public 不应残留业务表（除 shim 视图 / schema_migrations）
  PERFORM 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relkind IN ('r','p')
      AND c.relname NOT IN ('schema_migrations','layer_migration_map','migrate_log','verify_result');
  IF FOUND THEN
    INSERT INTO public.verify_result(chk,res) VALUES ('public 无残留业务表', 'FAIL(见下)');
  ELSE
    INSERT INTO public.verify_result(chk,res) VALUES ('public 无残留业务表', 'PASS');
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
