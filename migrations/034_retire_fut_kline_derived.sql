-- =============================================================================
-- 034 · G9-a：删除 l0_raw.fut_kline 中已退役的 continuous / cont_adj 派生数据
--              （20,209,818 行 / 约 4.2 GB）并退役其专属视图
-- 日期：2026-10-06（D3 裁定"与 G9 合并后再删"已满足）
-- =============================================================================
--
-- ⚠ 不可逆：本迁移删除真实数据行。执行前请确保已完成 G9 口径切换与验收。
--
-- 删除依据
-- --------
-- l0_raw.fut_kline 共 26,408,724 行，其中：
--   * cont_adj    10,123,979 行 —— 前复权链，G9 已退役不再刷新
--   * continuous  10,085,839 行 —— 派生连续，2026-10-04 G9 退役不再刷新
--   * contract     6,198,906 行 —— **真实合约行情，保留**
-- 即 76.5% 的表体是已退役的派生数据。表整体 5570 MB。
-- 表注释亦写明："continuous(已废弃派生连续,2026-10-04 G9 退役,不再刷新),
-- cont_adj(已退役前复权)"。
--
-- 依赖处理（关键）
-- ----------------
-- 依赖 l0_raw.fut_kline 的视图有 4 个（public shim + access 三个）：
--   * public.fut_kline                 —— 全量 shim（无 kind 过滤），**保留**
--                                         删除后只剩 contract 行，语义正确
--   * access.v_fut_kline_raw_hourly    —— kind='contract'，**保留**
--   * access.v_fut_kline_cont_hourly   —— kind='continuous' → **退役（删除）**
--   * access.v_fut_kline_cont_daily    —— kind='continuous' → **退役（删除）**
-- 后两个删行后会变成空视图；且它们只在 db_layering_verify.sql 与迁移脚本里
-- 被引用，**无任何 app 代码使用**（G9 后小时线主连已改读 hourly_bar，
-- 见 app/data/caliber.py ROUTES ("hourly","continuous")）。故随数据一并退役。
--
-- 幂等性
-- ------
-- 视图用 DROP VIEW IF EXISTS；删行用 DELETE ... WHERE kind IN (...)（已删再跑为 0 行）。
-- 每个阶段前写 migrate_log 留痕。
-- =============================================================================

SET statement_timeout = '1800s';   -- 2000 万行删除可能耗时数分钟

BEGIN;

CREATE TABLE IF NOT EXISTS public.migrate_log (ts timestamptz DEFAULT now(), tab text, action text, detail text);

-- -----------------------------------------------------------------------------
-- 1) 删除前留痕
-- -----------------------------------------------------------------------------
DO $$
DECLARE n_adj bigint; n_cont bigint; n_contract bigint;
BEGIN
  SELECT count(*) INTO n_adj   FROM l0_raw.fut_kline WHERE kind='cont_adj';
  SELECT count(*) INTO n_cont  FROM l0_raw.fut_kline WHERE kind='continuous';
  SELECT count(*) INTO n_contract FROM l0_raw.fut_kline WHERE kind='contract';
  INSERT INTO public.migrate_log(tab,action,detail) VALUES
    ('fut_kline','PRE_RETire',
     format('cont_adj=%s continuous=%s contract=%s (保留) size=%s',
            n_adj, n_cont, n_contract,
            pg_size_pretty(hypertable_size('l0_raw.fut_kline'::regclass))));
  RAISE NOTICE '034 删除前: cont_adj=% continuous=% contract=%(保留)',
    n_adj, n_cont, n_contract;
END $$;

-- -----------------------------------------------------------------------------
-- 2) 退役两个 continuous 专属视图（删行后它们必然为空）
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS access.v_fut_kline_cont_hourly;
DROP VIEW IF EXISTS access.v_fut_kline_cont_daily;
DROP VIEW IF EXISTS public.v_fut_kline_cont_hourly;
DROP VIEW IF EXISTS public.v_fut_kline_cont_daily;
INSERT INTO public.migrate_log(tab,action,detail)
  VALUES ('v_fut_kline_cont_*','DROP_VIEW','随 continuous 数据退役（G9 后小时线主连改读 hourly_bar）');

-- -----------------------------------------------------------------------------
-- 3) 删除已退役 kind 的行
-- -----------------------------------------------------------------------------
DO $$
DECLARE n_before bigint; n_after bigint; n_del bigint; t0 timestamptz := clock_timestamp();
BEGIN
  SELECT count(*) INTO n_before FROM l0_raw.fut_kline;

  DELETE FROM l0_raw.fut_kline WHERE kind IN ('continuous','cont_adj');

  -- ★ 不能用 GET DIAGNOSTICS ROW_COUNT：TimescaleDB 超表的 DELETE 会被重写为
  --   分块执行，ROW_COUNT 只捕获外层（实测云端报 deleted=121222，而实际删了 2021 万行）。
  --   必须**回查**计数。
  SELECT count(*) INTO n_after FROM l0_raw.fut_kline;
  n_del := n_before - n_after;
  INSERT INTO public.migrate_log(tab,action,detail)
    VALUES ('fut_kline','DELETE_DERIVED',
            format('deleted=%s (before=%s after=%s) in %s -- 计数由回查得出，非 ROW_COUNT',
                   n_del, n_before, n_after,
                   round(extract(epoch from (clock_timestamp()-t0))::numeric,1)||'s'));
  RAISE NOTICE '034: deleted % derived rows (before=% after=%) in %s',
    n_del, n_before, n_after,
    round(extract(epoch from (clock_timestamp()-t0))::numeric,1)||'s';
END $$;

COMMIT;

-- =============================================================================
-- ★ 必须额外执行的维护步骤（不能写在事务内）
-- -----------------------------------------------------------------------------
-- DELETE **不会归还磁盘空间**：被删行成为死元组，文件体积不变
--   （实测云端 DELETE 后库仍 25GB、fut_kline 仍 5570MB）。
-- 必须在本迁移之后单独执行（会取排他锁，请在维护窗口做）：
--
--     VACUUM (FULL, ANALYZE) l0_raw.fut_kline;
--
-- 实测效果：
--   云端  库 25GB -> 20GB，fut_kline 超表 5570MB -> 485MB
--   本地  库 14GB -> 8279MB，fut_kline 超表 7164MB -> 1529MB
-- =============================================================================

RESET statement_timeout;

-- =============================================================================
-- 验收 SQL（应用后逐条执行，括号内为期望）
-- =============================================================================
-- SELECT kind, count(*) FROM l0_raw.fut_kline GROUP BY kind;
--                                     -- 仅剩 contract 一行，count=6198906
-- SELECT count(*) FROM access.v_fut_kline_raw_hourly;   -- >0（contract 视图仍在）
-- SELECT count(*) FROM public.fut_kline;                -- 6198906（shim 仅剩 contract）
-- SELECT count(*) FROM pg_views WHERE viewname LIKE 'v_fut_kline_cont%';  -- 0（已退役）
-- SELECT pg_size_pretty(hypertable_size('l0_raw.fut_kline'::regclass));   -- 应显著下降
-- SELECT pg_size_pretty(pg_database_size('futures'));                     -- 应下降约 4GB
-- SELECT tab,action,detail FROM public.migrate_log WHERE tab LIKE 'fut_kline%' ORDER BY 2;
