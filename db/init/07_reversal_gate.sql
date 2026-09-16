-- =====================================================
-- PRD §18.9（v1.3）M5.5：信号门控列 + 新指标支持
-- backtest_detail.signaled / gate_reason：门控覆盖率统计
-- =====================================================
ALTER TABLE backtest_detail ADD COLUMN IF NOT EXISTS signaled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE backtest_detail ADD COLUMN IF NOT EXISTS gate_reason TEXT;
