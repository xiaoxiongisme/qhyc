-- =====================================================================
-- 058: 注册 member_net_ts_z（会员净持仓**时序** z）— A1 / CL-20261108-20
-- ---------------------------------------------------------------------
-- 解决什么问题
--   `scripts/compute_factor_value.py::_zscore` 只产出**横截面 z**
--   （groupby trade_date，衡量「该品种今天在全池里排第几」），而单品种择时门
--   （V5X 的 M5 会员净持仓门）需要的是**时序 z**（相对自身历史有多极端）。
--   一个字段名承载两种语义，消费方拿错类型时不会报错，只会静默失效。
--
-- 实测代价（2026-11-08，38 白名单，近 365 日，member_net_z 共 8,611 行）
--   · 逐品种 |z| >= 1 的占比为 **0%** 的品种：19 / 38
--   · 占比 < 5% 的品种：25 / 38
--   · avg|z| 区间：0.150 ~ 5.858（极度不均）
--   ⇒ 把它当「|z| >= 1 才开仓」的择时门，等于对这 19 个品种永久禁开。
--
-- 为什么新增 factor_id 而不是覆盖 member_net_z
--   横截面 z 有它自己的合法用途（品种间排序 / cross_rank_z 合成），
--   改它会造成已有消费方静默变义。故两者并存，命名上强制可辨：
--     member_net_z      横截面 z   version 1.0
--     member_net_ts_z   时序 z     version 1.1   ← 本迁移
--
-- enabled = false（注册即中性）
--   与 049 的先例一致：新因子先入库、不参与任何选品/门控，须另走回测闸门
--   与准入流程才能启用。本迁移只解决「口径存在且可被正确引用」。
--
-- 幂等：ON CONFLICT DO UPDATE，可重复应用。
-- =====================================================================

BEGIN;

INSERT INTO l2_adj.factor_registry
    (factor_id, name, category, description, data_sources,
     min_history_days, horizon, lag_days, enabled, default_weight, max_weight, version)
VALUES (
    'member_net_ts_z',
    '会员净持仓时序 z',
    'B',
    '期货公司会员净持仓（top5 多单-空单）相对**自身历史**的时序 z：'
    || 'groupby(symbol) 滚动 60 期、min_periods=20。用于单品种择时/门控（如 V5X M5）。'
    || '⚠ 与 member_net_z 的**横截面** z 语义不同：后者按交易日全池标准化，只用于品种间排序。'
    || '2026-11-08 实测：拿横截面 z 当择时门，38 白名单中 19 个品种 |z|>=1 占比为 0%（100% 误杀）。',
    ARRAY['member_position_rank_summary'],
    60,          -- min_history_days：与滚动窗 60 期一致
    'daily',
    0,           -- lag_days：available_at = trade_date，无额外滞后
    false,       -- enabled：注册即中性，启用须过回测闸门
    0,
    0.3,
    '1.1'
)
ON CONFLICT (factor_id) DO UPDATE SET
    description = EXCLUDED.description,
    data_sources = EXCLUDED.data_sources,
    min_history_days = EXCLUDED.min_history_days,
    version = EXCLUDED.version;

COMMIT;
