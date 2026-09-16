-- =====================================================
-- PRD §17 工单①②：口径声明字段（caliber）
-- 全链路声明涨跌/幅度标签口径（label_metric，当前 close）
-- =====================================================
ALTER TABLE prediction_result ADD COLUMN IF NOT EXISTS caliber TEXT NOT NULL DEFAULT 'close';
ALTER TABLE briefing_signal  ADD COLUMN IF NOT EXISTS caliber TEXT NOT NULL DEFAULT 'close';
ALTER TABLE backtest_result  ADD COLUMN IF NOT EXISTS caliber TEXT NOT NULL DEFAULT 'close';
ALTER TABLE backtest_detail  ADD COLUMN IF NOT EXISTS caliber TEXT NOT NULL DEFAULT 'close';
