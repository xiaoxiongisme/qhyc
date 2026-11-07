-- =============================================================================
-- 049 · 封禁品种状态对齐 + 基差时序因子注册 + 因子/汇总作业登记
-- 日期：2026-11-06　CB 三仓全方位体检 · 接线批次
-- 适用：本地库与云端库同构执行（幂等）
--
-- 背景（三项，均为「看起来配了、实际没生效」型缺陷）
-- ------------------------------------------------------------------
--  1) dim_variety 里 B / CS 仍在且 is_active=true，而两者都在
--     app/ingest/blocked_varieties.py 的封禁清单里（用户 2026-10-10 剔除）。
--     scripts/rebuild_dict_from_akshare.py 重建时会 DELETE 封禁品种行
--     （预清 + is_blocked 跳过），故长期看会被删；但**当前**状态是"封禁未生效"。
--     处置：本迁移把 is_active 置 false（与封禁清单对齐，保留行可审计）。
--     ⚠ 真身是 l3_ref.dim_variety（BASE TABLE）；public.dim_variety 是同名 VIEW，
--       非限定名会命中视图 —— 故本迁移**显式查基表 schema** 后再 UPDATE。
--
--  2) app/features/position_factors.py::basis_factors 此前**无任何调用方**
--     （死代码）。本迁移注册两个由它派生的因子，为
--     scripts/compute_factor_value.py 的 C 组（基差时序）提供注册身份。
--     注册即中性（enabled=false），待回测闸门 + 准入后再开启。
--
--  3) 因子/汇总作业从未登记进 ops.scheduled_task，调度器里也没有对应作业：
--       · scripts/compute_factor_value.py —— 产出 22 个已注册因子
--         （A 组 11 + B 组 11），实测全部停在 2026-09-30（脚本从未被调度）；
--       · scripts/collect_member_rank_summary.py —— 产出 member_position_rank_summary
--         （B 组 member_* / vol_concentration_* 的唯一上游），同样停更到 09-30。
--     本迁移补登记（字典面）；代码面的 APScheduler 注册在 app/scheduler.py。
-- =============================================================================

BEGIN;

-- 1. 封禁品种 is_active=false（B / CS）
DO $$
DECLARE
    target text;
    n int;
BEGIN
    SELECT table_schema INTO target
      FROM information_schema.tables
     WHERE table_name = 'dim_variety' AND table_type = 'BASE TABLE'
     ORDER BY (table_schema = 'l3_ref') DESC
     LIMIT 1;
    IF target IS NULL THEN
        RAISE NOTICE 'dim_variety 基表未找到 → 跳过封禁状态对齐';
        RETURN;
    END IF;
    EXECUTE format(
        'UPDATE %I.dim_variety SET is_active = false, updated_at = now() '
        'WHERE upper(variety_code) IN (''B'', ''CS'') AND is_active', target);
    GET DIAGNOSTICS n = ROW_COUNT;
    RAISE NOTICE 'dim_variety(%) 封禁品种 is_active=false：% 行', target, n;
END $$;

-- 2. 注册基差时序因子（basis_factors 派生的两个）
INSERT INTO factor_registry
  (factor_id, name, category, description, data_sources,
   min_history_days, horizon, lag_days, enabled, default_weight, max_weight, version)
VALUES
  ('f_basis_mean60_z', '基差60日均值Z', 'B',
   '基差率 60 日均值的横截面 z（§18.5 M6a basis_factors 派生）',
   '{spot_basis}', 60, 'B', 1, false, 0, 0.02, '1.0'),
  ('f_basis_chg5_z',   '基差5日变化Z',  'B',
   '基差率 5 日变化的横截面 z（§18.5 M6a basis_factors 派生；基差走强=升水扩大）',
   '{spot_basis}', 60, 'B', 1, false, 0, 0.02, '1.0')
ON CONFLICT (factor_id) DO UPDATE
   SET data_sources = EXCLUDED.data_sources,
       category     = EXCLUDED.category,
       description  = EXCLUDED.description,
       horizon      = EXCLUDED.horizon;

-- 额度自检：factor_registry.max_weight 之和必须 ≤ 1.0（app/factor/asof.py 启动校验）
DO $$
DECLARE
    s numeric;
BEGIN
    SELECT sum(max_weight) INTO s FROM factor_registry;
    IF s > 1.0 THEN
        RAISE EXCEPTION 'max_weight 之和 % 超过上限 1.0，已回滚', round(s, 4);
    END IF;
    RAISE NOTICE 'factor_registry max_weight 之和 = % / 1.0 OK', round(s, 4);
END $$;

-- 3. 作业字典补登记（ops.scheduled_task）
--    ⚠ 只补**代码侧拥有**的最小行；已有行（含人控字段 enabled/owner/tags）绝不覆盖。
INSERT INTO ops.scheduled_task
  (task_id, name, purpose, executor, schedule, schedule_desc, target, location, enabled, expected_gap_days)
VALUES
  ('factor_value_daily',
   'A组因子日计算',
   '每日 16:35 计算 A 组 11 个日内价量因子并落 factor_value（受 factor_value_enabled 开关；'
   '此前该脚本从未被调度 → 11 个因子实测停在 2026-09-30）',
   'apscheduler', '35 16 * * *', '每日 16:35',
   'app/scheduler.py::_factor_value_job', 'cloud-container', TRUE, 3),
  ('collect_member_rank_summary_daily',
   '会员持仓排名汇总',
   '每日 17:45 由 member_position_rank 聚合出 member_position_rank_summary'
   '（B 组 member_*/vol_concentration_* 的唯一上游；此前从未被调度 → 上游停更到 09-30）',
   'apscheduler', '45 17 * * *', '每日 17:45',
   'app/scheduler.py::_collect_member_rank_summary_job', 'cloud-container', TRUE, 3),
  ('factor_value_b_daily',
   'B/C组因子日计算',
   '每日 18:00 计算 B 组（基本面 11 个）与 C 组（基差时序 2 个）因子并落 factor_value'
   '（依赖 17:45 的 member_position_rank_summary；受 factor_value_enabled 开关）',
   'apscheduler', '0 18 * * *', '每日 18:00',
   'app/scheduler.py::_factor_value_b_job', 'cloud-container', TRUE, 3)
ON CONFLICT (task_id) DO NOTHING;

COMMIT;
