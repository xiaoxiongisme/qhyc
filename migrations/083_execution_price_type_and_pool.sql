-- 083_execution_price_type_and_pool.sql
-- CL-20261121-05 / 5M-P0B：限价单价格类型全链路落地；
-- CL-20261121-06 / 5M-P0C：池级单持仓归属（独立开关，默认关闭）。
--
-- 背景（2026-11-21 只读探查实证）
-- ----------------------------------------------------
-- SubmitOrderReq / persistence / DDL / broker payload 均无 price_type：
-- CTP 网关把 OrderPriceType 硬编码为 LimitPrice，sim_broker 立即全额成交，
-- 于是"限价挂单改善入场价"这一 5m 策略全部 edge 在模拟/审计中都测不出来。
-- 另 position_sync_enabled=false（CL-20261108-26 用户拍板），execution_position
-- 不是权威真源，且"多品种池只持一仓"没有现成表达。
--
-- 【默认兼容】两列都带 DEFAULT：存量单不改、旧调用方（stop_loss.py）不改造也能落库；
-- 新开关默认 false：现有 V5X / V3.4 / 执行桥行为逐位不变。
-- 本迁移不在发布时自动执行，须显式 scripts/db_apply_migrations.py --only 083_*.sql --apply。

BEGIN;

ALTER TABLE public.execution_order
    ADD COLUMN IF NOT EXISTS price_type TEXT NOT NULL DEFAULT 'LIMIT';

ALTER TABLE public.execution_order
    ADD COLUMN IF NOT EXISTS pool_id TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'execution_order_price_type_chk'
           AND conrelid = 'public.execution_order'::regclass
    ) THEN
        ALTER TABLE public.execution_order
            ADD CONSTRAINT execution_order_price_type_chk
            CHECK (price_type IN ('LIMIT', 'MARKET'));
    END IF;
END $$;

COMMENT ON COLUMN public.execution_order.price_type IS
    'CL-20261121-05：LIMIT=挂限价单（默认，与既有行为一致）；MARKET=市价。'
    '网关侧必须真实映射 OrderPriceType，不接受静默按市价成交。';
COMMENT ON COLUMN public.execution_order.pool_id IS
    'CL-20261121-06：5m 策略池级单持仓归属。NULL=普通单，不启用池级闸。';

CREATE INDEX IF NOT EXISTS idx_execution_order_pool_status
    ON public.execution_order (pool_id, status)
    WHERE pool_id IS NOT NULL;

-- 池级闸独立开关：不复用 position_sync_enabled（后者语义是回写 execution_position）。
INSERT INTO cfg_feature_switch
    (switch_key, switch_name, switch_group, enabled, value, description)
VALUES
    (
        'five_min_pool_position_enabled',
        '5m 策略池级单持仓闸（同一 pool_id 只允许一笔未平仓单）',
        'strategy',
        false,
        '',
        'CL-20261121-06。false=池级闸不生效（现状逐位不变）；'
        || 'true=开仓前查池内在场，只允许同仓平仓。不写 execution_position / fusion_position，'
        || '重启时从 execution_order 未终态单重建。'
    )
ON CONFLICT (switch_key) DO NOTHING;

COMMIT;
