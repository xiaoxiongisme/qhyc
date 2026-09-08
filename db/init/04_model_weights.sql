-- =====================================================
-- M4：⑳ 模型权重表（按近 60 日回测准确率月更）
-- =====================================================
CREATE TABLE IF NOT EXISTS model_weights (
    model       TEXT         PRIMARY KEY,
    weight      NUMERIC(8,4) NOT NULL,
    dir_acc     NUMERIC(6,4),                       -- 最近 60 评估点方向准确率
    sample_n    INT,
    source      TEXT         NOT NULL DEFAULT 'backtest',  -- backtest/config
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
