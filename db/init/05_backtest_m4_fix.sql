-- =====================================================
-- M4 审计修复（P0/P1-4/P2-7）：
-- 1) backtest_result 主键改 (run_id, symbol, model)，加 symbol 列
-- 2) 新表 backtest_detail（per-eval-point 明细，支撑看板下钻/误判归因）
-- 注：旧数据全部清洗（P0 互相覆盖已失真，修复后全量重跑）
-- =====================================================

DROP TABLE IF EXISTS backtest_result;

CREATE TABLE backtest_result (
    run_id       TEXT         NOT NULL,
    symbol       TEXT         NOT NULL,
    model        TEXT         NOT NULL,
    window_len   INT          NOT NULL DEFAULT 250,   -- window 为 PG 保留字
    start_date   DATE,
    end_date     DATE,
    dir_acc      NUMERIC(6,4),
    mae          NUMERIC(12,6),
    rmse         NUMERIC(12,6),
    quantile_hit NUMERIC(6,4),
    sample_n     INT,
    by_state     JSONB,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, symbol, model)
);
CREATE INDEX IF NOT EXISTS idx_bt_result_symbol ON backtest_result (symbol);
CREATE INDEX IF NOT EXISTS idx_bt_result_model  ON backtest_result (model);

-- per-eval-point 明细（§16 P2-7）
CREATE TABLE IF NOT EXISTS backtest_detail (
    run_id      TEXT         NOT NULL,
    symbol      TEXT         NOT NULL,
    model       TEXT         NOT NULL,
    eval_date   DATE         NOT NULL,
    state       TEXT,
    pred_dir    TEXT,
    prob        NUMERIC(6,4),
    point       NUMERIC(12,6),
    low         NUMERIC(12,6),
    high        NUMERIC(12,6),
    actual      NUMERIC(12,6),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, symbol, model, eval_date)
);
CREATE INDEX IF NOT EXISTS idx_bt_detail_symbol_date ON backtest_detail (symbol, eval_date);

TRUNCATE model_weights;  -- P1-2：清空被污染权重，回退 config 等权（修复后由整批 run 重新生成）