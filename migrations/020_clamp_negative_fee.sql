-- 020_clamp_negative_fee.sql
-- 钳制非法费率：任何 fee_value / broker_markup_value 为**负**的行都是错误数据。
--
-- 背景（2026-10-03 云端体检发现）：
--   RU / SC / SS / T 的 CLOSE_TODAY 为 FREE，但 fee_value = **-0.01**。
--   成因：akshare 的平今费为 0，播种脚本统一「减去券商加收 0.01」后变负。
--   后果：回测/实盘按此计算会**倒贴钱**（成本为负），且完全静默。
--   —— 这正是本项目最典型的「静默失效」模式（见 memory）。
--
-- 规则：
--   * fee_type = 'FREE'  → fee_value 必须恰为 0，且券商加收必须为 0（免费即全免）。
--   * 其余类型          → fee_value / broker_markup_value 下限钳到 0（不允许负费率）。

UPDATE dim_trading_cost
SET fee_value = 0,
    exchange_fee_value = 0,
    broker_markup_type = 'NONE',
    broker_markup_value = 0,
    note = COALESCE(note, '') || ' | 020: FREE 行负值钳制为 0',
    updated_at = now()
WHERE fee_type = 'FREE'
  AND (fee_value < 0 OR exchange_fee_value < 0 OR broker_markup_value < 0);

UPDATE dim_trading_cost
SET fee_value = 0,
    broker_markup_value = CASE WHEN broker_markup_value < 0 THEN 0 ELSE broker_markup_value END,
    note = COALESCE(note, '') || ' | 020: 负费率钳制为 0',
    updated_at = now()
WHERE fee_type <> 'FREE' AND fee_value < 0;

-- 断言：改后不应残留任何负费率
DO $$
DECLARE bad int;
BEGIN
    SELECT count(*) INTO bad FROM dim_trading_cost
    WHERE fee_value < 0 OR broker_markup_value < 0
       OR exchange_fee_value < 0;
    IF bad > 0 THEN
        RAISE EXCEPTION '仍存在负费率 % 行，钳制未生效', bad;
    END IF;
END $$;

COMMENT ON COLUMN dim_trading_cost.fee_value IS
    '费率值。FIXED=元/手；PCT=‱(万分之一)。**不得为负**（020 起钳制）';
