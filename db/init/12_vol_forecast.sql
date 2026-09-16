-- =====================================================
-- PRD §18.7（v1.3.2）M6c：波动率产品化（P8）
-- prediction_result 扩展波动率预测三件套：
--   vol_point: 次日条件波动率 σ_t 预测（%）
--   vol_low / vol_high: 次日 |收益| 的 [P5, P95] 区间（半正态分位）
-- 回测指标（vol_hit / vol_rmse）入 backtest_result.by_state JSON
-- =====================================================
ALTER TABLE prediction_result ADD COLUMN IF NOT EXISTS vol_point NUMERIC(12, 4);
ALTER TABLE prediction_result ADD COLUMN IF NOT EXISTS vol_low   NUMERIC(12, 4);
ALTER TABLE prediction_result ADD COLUMN IF NOT EXISTS vol_high  NUMERIC(12, 4);
