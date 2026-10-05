-- =============================================================================
-- 033 · 空间回收：删除两张已确认死表（fut_kline_drv / bar_5m_dirty_bak）
-- 日期：2026-10-06（用户批准"确定死可立刻回收 3.47GB"）
-- =============================================================================
--
-- 删除依据（死资产审计，DB 侧 + 代码侧双向确认）
-- -----------------------------------------------------------------------------
-- 1) public.fut_kline_drv  19,935,980 行 / 3410 MB
--    * 全仓零引用：app/ scripts/ db/init+migrations/ docs/ 均无命中；
--      无视图、无函数依赖（pg_depend 检查 NONE）
--    * 与 l0_raw.fut_kline 重叠 1204 万行，且**缺** contract hourly(527万) 与
--      contract min15(19万)、continuous 段也更旧 → 是分层迁移期的**陈旧副本**，
--      权威版本是 l0_raw.fut_kline
--
-- 2) public.bar_5m_dirty_bak  532,029 行 / 61 MB
--    * 唯一创建者 scripts/repair_bar5m_dirty.py（该脚本自述"先备份受影响行…
--      可回滚"），app 零引用，无视图依赖
--    * 与 l1_mkt.bar_5m **零重叠**：它保存的是清洗时**被移除的脏行**
--      （2015-01-05 ~ 2024-12-26、63 个符号）
--    ⚠ 删除后该次修复的**回滚能力消失**。故本迁移在删除前把行数/日期范围/
--      符号数写入 migrate_log 留痕，保证审计可追溯。
--
-- 本次**不删**的对象（原计划在内，经复核调整）
-- -----------------------------------------------------------------------------
-- * l3_ref.cfg_holiday(0行)：其上有 public.cfg_holiday **兼容视图依赖**，
--   删表会断视图；且 0 行对空间无贡献。
-- * public.briefing_signal / portfolio_equity / pipeline_push_log(均 0 行)：
--   0 MB、对 3.47GB 无贡献，但有 ORM 模型（app/models/domain.py），
--   删表反而可能在 create_all / 查询时报 "does not exist"。保留为结构对象。
-- → 结论：3.47GB 的空间收益**全部来自上面两张表**，上述对象是否删除待另行裁定。
--
-- 幂等性
-- ------
-- 均用 DROP TABLE IF EXISTS；已删则跳过并记 SKIP。
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.migrate_log (ts timestamptz DEFAULT now(), tab text, action text, detail text);

-- -----------------------------------------------------------------------------
-- 1) fut_kline_drv
-- -----------------------------------------------------------------------------
DO $$
DECLARE n bigint; sz text;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname='public' AND c.relname='fut_kline_drv' AND c.relkind IN ('r','p')) THEN
    EXECUTE 'SELECT count(*) FROM public.fut_kline_drv' INTO n;
    sz := pg_size_pretty(pg_total_relation_size('public.fut_kline_drv'::regclass));
    INSERT INTO public.migrate_log(tab,action,detail)
      VALUES ('fut_kline_drv','PRE_DROP', format('rows=%s size=%s (陈旧副本，权威版 l0_raw.fut_kline)', n, sz));
    DROP TABLE public.fut_kline_drv;
    INSERT INTO public.migrate_log(tab,action,detail)
      VALUES ('fut_kline_drv','DROP','reclaimed ' || sz);
    RAISE NOTICE '033: dropped public.fut_kline_drv (rows=% size=%)', n, sz;
  ELSE
    INSERT INTO public.migrate_log(tab,action,detail)
      VALUES ('fut_kline_drv','SKIP','already dropped');
  END IF;
END $$;

-- -----------------------------------------------------------------------------
-- 2) bar_5m_dirty_bak（删除前先留痕：行数 / 日期范围 / 符号数）
-- -----------------------------------------------------------------------------
DO $$
DECLARE n bigint; sz text; dmin date; dmax date; nsym bigint;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname='public' AND c.relname='bar_5m_dirty_bak' AND c.relkind IN ('r','p')) THEN
    EXECUTE 'SELECT count(*), min(bucket)::date, max(bucket)::date, count(DISTINCT symbol)
               FROM public.bar_5m_dirty_bak' INTO n, dmin, dmax, nsym;
    sz := pg_size_pretty(pg_total_relation_size('public.bar_5m_dirty_bak'::regclass));
    INSERT INTO public.migrate_log(tab,action,detail)
      VALUES ('bar_5m_dirty_bak','PRE_DROP',
              format('rows=%s dates=%s..%s symbols=%s size=%s (repair_bar5m_dirty 的回滚备份，清洗时被移除的脏行)',
                     n, dmin, dmax, nsym, sz));
    DROP TABLE public.bar_5m_dirty_bak;
    INSERT INTO public.migrate_log(tab,action,detail)
      VALUES ('bar_5m_dirty_bak','DROP','reclaimed ' || sz);
    RAISE NOTICE '033: dropped public.bar_5m_dirty_bak (rows=% size=%)', n, sz;
  ELSE
    INSERT INTO public.migrate_log(tab,action,detail)
      VALUES ('bar_5m_dirty_bak','SKIP','already dropped');
  END IF;
END $$;

COMMIT;

-- =============================================================================
-- 验收 SQL（应用后逐条执行，括号内为期望）
-- =============================================================================
-- SELECT count(*) FROM pg_class WHERE relname='fut_kline_drv';        -- 0
-- SELECT count(*) FROM pg_class WHERE relname='bar_5m_dirty_bak';     -- 0
-- SELECT pg_size_pretty(pg_database_size('futures'));                  -- 应下降约 3.4GB
-- SELECT tab, action, detail FROM public.migrate_log
--   WHERE tab IN ('fut_kline_drv','bar_5m_dirty_bak') ORDER BY 1,2;    -- 留痕可查
