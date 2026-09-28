-- =============================================================================
-- 005 · 因子共线治理：inventory_z 退役（不删数据）
-- 日期：2026-09-29
-- 适用：本地库与云端库同构执行（幂等）
--
-- 实证依据（2026-09-29 实测，本地库）
--   1. 源表覆盖悬殊：
--        inventory          2,452 行 / 42 品种 / 2026-05-26~2026-09-24（仅 4 个月）
--        warehouse_receipt 720,967 行 / 77 品种 / 2020-07-02~2026-09-24
--   2. 两者**真共线**（不是口径 bug）：
--        - 定义取自不同列（inventory_qty vs receipt_qty），z 值相等比例 = 0.0000，
--          即不是同一份数据被算了两次；
--        - 但按品种的相关性几乎全线极高：AL888 0.991 / CU888 0.990 / A888 0.960 /
--          I888 0.911 / J888 0.897 / C888 0.943（仅 BU888 0.252 例外），
--          pooled |ρ| = 0.96 —— 仓单是可交割库存的凭证，多数品种上近乎同一信息。
--   3. inventory_z 自身显著性不足：IC(h=5) = -0.058，t = -1.76（|t| < 2）。
--
-- 决策
--   * inventory_z → enabled=false、max_weight=0（**保留历史值，不删数据**）；
--     同时计入仓单+库存等于把同一信息重复加权。
--   * warehouse_receipt_z 保留（长历史、71 品种、IC(h=5)=0.031 / t=4.87）。
--   * 释放出的 0.08 额度留给后续准入的因子。
--
-- 回滚
--   UPDATE factor_registry SET enabled=true, max_weight=0.08 WHERE factor_id='inventory_z';
--   （须同时确认 sum(max_weight) ≤ 1.0）
-- =============================================================================

BEGIN;

UPDATE factor_registry
   SET enabled = false,
       max_weight = 0,
       description = coalesce(description, '')
                     || ' 【2026-09-29 退役：源表仅覆盖 2026-05~09(4个月)，且与 '
                        'warehouse_receipt_z 共线 pooled |ρ|=0.96（多品种 >0.9）；'
                        'IC(h=5)=-0.058/t=-1.76 不足。数据保留，可回滚。】'
 WHERE factor_id = 'inventory_z';

-- 额度自检：退役后之和必须 ≤ 1.0（app/factor/asof.validate_max_weight）
DO $$
DECLARE
    s numeric;
BEGIN
    SELECT sum(max_weight) INTO s FROM factor_registry;
    IF s > 1.0 THEN
        RAISE EXCEPTION 'max_weight 之和 % 超过上限 1.0，已回滚', round(s, 4);
    END IF;
    RAISE NOTICE '退役后 max_weight 之和 = % / 1.0 OK', round(s, 4);
END $$;

COMMIT;
