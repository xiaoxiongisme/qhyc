-- =============================================================================
-- 004 · 监控与风控支撑表（Phase 4 / Phase 5）
-- 日期：2026-09-29
-- 适用：本地库与云端库同构执行（幂等）
--
-- 内容
--   1. factor_ic_roll   —— 因子滚动 IC（T21 监控）：准入/淘汰的量化依据
--   2. portfolio_equity —— 组合权益曲线（V5 组合回撤熔断的唯一真源）
--
-- 设计取舍
--   * 两张都是**小表**（因子数 × 交易日 / 交易日），可放心加外键与唯一约束；
--     亿级行情表仍不加外键（理由见 001 迁移头部）。
--   * portfolio_equity 用 (trade_date) 作主键，UPSERT 幂等，
--     使「每日结算后写一行」的重跑不会重复放大 PnL。
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. factor_ic_roll：因子滚动 IC
--    口径：spearman(因子 z, 未来 h 日收益)，滚动 window 个交易日（默认 20）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS factor_ic_roll (
    factor_id   text        NOT NULL,
    trade_date  date        NOT NULL,
    win_days    int         NOT NULL DEFAULT 20,   -- 不用 window：PG 保留字
    horizon     int         NOT NULL DEFAULT 1,
    ic          numeric(12, 6),
    icir        numeric(12, 6),
    t_stat      numeric(12, 6),
    n_symbols   int,
    method      text        NOT NULL DEFAULT 'spearman',
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (factor_id, trade_date, win_days, horizon)
);

CREATE INDEX IF NOT EXISTS ix_factor_ic_roll_recent
    ON factor_ic_roll (trade_date DESC, factor_id);

COMMENT ON TABLE factor_ic_roll IS
    '因子滚动 IC（T21）。判据：|IC|<0.02 或 ICIR 连续翻号 → 触发复核；'
    '20 日滚动均值低于 0.01 且方向与注册方向相反 → 建议降级（enabled=false）。';

-- 外键：因子必须已在注册表内（防止"有数据没注册"的孤儿再次出现）
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_factor_ic_roll_registry') THEN
        ALTER TABLE factor_ic_roll
            ADD CONSTRAINT fk_factor_ic_roll_registry
            FOREIGN KEY (factor_id) REFERENCES factor_registry(factor_id)
            ON UPDATE CASCADE ON DELETE CASCADE;
        RAISE NOTICE 'factor_ic_roll → factor_registry 外键已建立';
    ELSE
        RAISE NOTICE 'factor_ic_roll → factor_registry 外键已存在，跳过';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. portfolio_equity：组合权益曲线（V5 熔断输入）
--    口径：trade_date 唯一；realized_pnl 为当日已实现盈亏，equity 为累计权益
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS portfolio_equity (
    trade_date   date      NOT NULL,
    equity       numeric(18, 2),
    realized_pnl numeric(18, 2),
    note         text,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date)
);

COMMENT ON TABLE portfolio_equity IS
    '组合权益曲线（V5 组合回撤熔断 app/risk/portfolio_brake.py 的输入）。'
    '缺失时熔断模块降级为「不熔断」并记录 WARNING（不阻断决策链）。';

COMMIT;
