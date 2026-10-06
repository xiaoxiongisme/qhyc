-- =============================================================================
-- 035 · G9-b：删除 public.minute_bar_adj（58,620,697 行 / 9289 MB）
--             及随之失去用途的 public.minute_bar_stage
-- 日期：2026-10-06（D3 裁定"与 G9 合并后再删"的前置已满足）
-- =============================================================================
--
-- ⚠ 不可逆：本迁移 DROP 真实表。
--
-- 解锁前置已满足（逐条核对）
-- --------------------------
-- PRD docs/期货量化交易系统PRD.md:250 要求："必须先完成 G9 对 fut_kline.continuous 的
-- 退役/重定位裁定，再一次性合并处置"。
--   ✅ fut_kline.continuous + cont_adj 已在 034 删除（20,209,818 行）
--   ✅ 生成作业已退役：app/scheduler.py:1103-1106 "1 分钟复权作业(adjust_minute_bar_adj)
--      已于 2026-10-03 退役…不再注册"
--   ✅ 开关已标废弃：migrations/008_cfg_tables.sql:63 minute_bar_adj_enabled
--      'minute_bar_adj 已废弃，保留开关便于审计'
--   ✅ 口径路由已摘除：app/data/caliber.py:75 ("1m","cont_adj") Route("minute_bar_adj")
--      处于**注释**状态
--   ✅ 无视图依赖：pg_depend/pg_rewrite 查询 0 行
--      （此前命中的 _timescaledb_internal._direct_view_7/_partial_view_7 是
--       超表自身内部视图，随 DROP 一并消失，无需单独处理）
--
-- 删除顺序说明（PRD 强调"顺序不可逆"）
-- ------------------------------------
-- minute_bar_adj 是 cont_adj 的**上游**。本轮顺序为：
--   034 先删下游（fut_kline.continuous / cont_adj）
-- → 035 再删上游（minute_bar_adj）
-- 若反过来先删上游，下游 cont_adj 将无法重建/追溯。顺序正确。
--
-- 空间
-- ----
-- DROP TABLE 会**立即删除数据文件**（与 DELETE 不同，无需再 VACUUM）。
-- 预计云端库 20GB -> 约 11GB。
--
-- 连带
-- ----
-- public.minute_bar_stage（0 行 / 16 kB）是 adjust_minute 的写入暂存表；
-- 生成作业退役 + minute_bar_adj 删除后它已无任何用途，一并删除消除误导。
--
-- ⚠ 迁移后失效的脚本（需另行归档，不在本迁移范围）
--    scripts/adjust_minute.py  —— 仍引用已删除的 minute_bar_adj
--    scripts/db_audit_constraints.py —— 列名清单里仍有 minute_bar_adj
--    （两者均已在死资产审计中列为"确定死/待归档"）
--
-- 幂等性
-- ------
-- DROP TABLE IF EXISTS；已删则记 SKIP。
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.migrate_log (ts timestamptz DEFAULT now(), tab text, action text, detail text);

-- -----------------------------------------------------------------------------
-- 1) 删除前留痕 + 安全检查（存在意外依赖则中止）
-- -----------------------------------------------------------------------------
DO $$
DECLARE n bigint; sz text; dep int;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname='public' AND c.relname='minute_bar_adj' AND c.relkind IN ('r','p')) THEN

    SELECT count(*) INTO n FROM public.minute_bar_adj;
    sz := pg_size_pretty(hypertable_size('public.minute_bar_adj'::regclass));

    -- 安全闸：若还有**用户视图**依赖它，拒绝删除（内部视图不算）
    SELECT count(*) INTO dep
    FROM pg_depend d JOIN pg_rewrite r ON r.oid=d.objid
    JOIN pg_class dv ON dv.oid=r.ev_class JOIN pg_class src ON src.oid=d.refobjid
    JOIN pg_namespace dn ON dn.oid=dv.relnamespace
    WHERE d.classid='pg_rewrite'::regclass AND src.relname='minute_bar_adj'
      AND dn.nspname NOT LIKE '\_timescaledb%' AND dn.nspname NOT LIKE 'timescaledb%';

    IF dep > 0 THEN
      RAISE EXCEPTION '035 中止：minute_bar_adj 仍有 % 个用户视图依赖，请先处理', dep;
    END IF;

    INSERT INTO public.migrate_log(tab,action,detail)
      VALUES ('minute_bar_adj','PRE_DROP',
              format('rows=%s size=%s deps=0 (前复权上游；下游 cont_adj 已于 034 删除)', n, sz));
    RAISE NOTICE '035 删除前: rows=% size=% (无用户视图依赖)', n, sz;
  ELSE
    INSERT INTO public.migrate_log(tab,action,detail)
      VALUES ('minute_bar_adj','SKIP','already dropped');
  END IF;
END $$;

-- -----------------------------------------------------------------------------
-- 2) 删除（DROP 立即归还磁盘空间，无需 VACUUM）
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS public.minute_bar_adj;
DROP TABLE IF EXISTS public.minute_bar_stage;

INSERT INTO public.migrate_log(tab,action,detail)
  VALUES ('minute_bar_adj','DROP','dropped (upstream of retired cont_adj)'),
         ('minute_bar_stage','DROP','dropped (staging for retired adjust_minute)');

COMMIT;

-- =============================================================================
-- 验收 SQL（应用后逐条执行，括号内为期望）
-- =============================================================================
-- SELECT count(*) FROM pg_class WHERE relname='minute_bar_adj';      -- 0
-- SELECT count(*) FROM pg_class WHERE relname='minute_bar_stage';    -- 0
-- SELECT pg_size_pretty(pg_database_size('futures'));                 -- 应降约 9GB（20GB->~11GB）
-- SELECT count(*) FROM l0_raw.minute_bar;                             -- 119538807（上游原始数据保留）
-- SELECT tab,action,detail FROM public.migrate_log
--   WHERE tab LIKE 'minute_bar%' ORDER BY 1;                          -- 留痕可查
