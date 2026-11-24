-- 087_five_min_exit_chain.sql
-- CL-20261124-06：P3 闭环——5m 模拟盘离场执行链路。
--
-- 背景（实证）：five_min_scan 只消费开仓意图落 SIM 单，意图里的 exit_plan 只是
-- 元数据，无人落库也无人消费；exit_plan / exit_action 表均 0 行，pool=5m-sim
-- 的 6 笔订单全 OPEN FILLED、无 CLOSE。P3 校准（CL-20261122-01）无法积累
-- 离场口径样本。本项目已有完整决策层 exit_guard + risk_gate /exit/*（只出建议），
-- 缺的是「开仓写计划 + 盘中判触发下 CLOSE 单」两环。
--
-- 【口径隔离】不复用通用 exit_plan 表：其 UNIQUE(account, real_symbol, direction)
-- 与分批止盈/时间止损/吊灯字段是 V3.4 账户级口径，而 5m 研究口径是
-- 1R=3ATR / TP=2.5R / tp1 禁用 / 二选一全手数平仓（5M-EXIT-CALIBER 守卫钉死）。
-- 混写会污染单一真源，故建专用表。
--
-- 【默认兼容】新开关 five_min_exit_scan_enabled 默认 false，作业整轮 return；
-- 实盘开关 five_min_pool_position_enabled 不动（保持 off）；EXECUTION_ENABLED 不动。
-- 下 CLOSE 单强制走 /execution/sim/orders（channel=SIM + broker=sim）。
--
-- 本迁移不在发布时自动执行，须显式：
--   docker exec -i qhyc-timescaledb psql -U futures -d futures -f /app/migrations/087_five_min_exit_chain.sql

BEGIN;

-- 5m 离场计划：一笔开仓一行，决策层 evaluate 的入参快照。
CREATE TABLE IF NOT EXISTS five_min_exit_plan (
    id                  BIGSERIAL PRIMARY KEY,
    -- 幂等：一个开仓信号只建一份计划（与 execution_order.signal_id 同源）
    signal_id           TEXT        NOT NULL UNIQUE,
    open_order_id       BIGINT,     -- 对应开仓 execution_order.id（审计用）
    symbol_continuous   TEXT        NOT NULL,   -- 取价口径（SR888，raw continuous）
    real_symbol         TEXT        NOT NULL,   -- 下单口径（SR2701）
    exchange            TEXT        NOT NULL,
    direction           TEXT        NOT NULL CHECK (direction IN ('LONG','SHORT')),
    lots                INTEGER     NOT NULL CHECK (lots > 0),
    entry_px            NUMERIC     NOT NULL,
    init_stop           NUMERIC     NOT NULL,
    r_unit              NUMERIC     NOT NULL CHECK (r_unit > 0),
    tp_px               NUMERIC     NOT NULL,   -- 2.5R 止盈目标（entry±2.5*r_unit）
    account             TEXT        NOT NULL DEFAULT 'SIM',
    channel             TEXT        NOT NULL DEFAULT 'SIM',
    pool_id             TEXT,
    status              TEXT        NOT NULL DEFAULT 'OPEN'
                        CHECK (status IN ('OPEN','CLOSED','CANCELED')),
    close_order_id      BIGINT,     -- 平仓单 id（成交后回填）
    close_kind          TEXT,       -- HARD_STOP / TP（二选一）
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at           TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_five_min_exit_plan_open
    ON five_min_exit_plan (status) WHERE status = 'OPEN';

COMMENT ON TABLE five_min_exit_plan IS
    'CL-20261124-06：5m 模拟盘离场计划（5M-EXIT-CALIBER 口径），与 V3.4 exit_plan 隔离';
COMMENT ON COLUMN five_min_exit_plan.r_unit IS '1R=3ATR 的价格距离；tp_px=entry±2.5R';
COMMENT ON COLUMN five_min_exit_plan.signal_id IS '与开仓单 signal_id 同源，保证一信号一计划';

-- 离场动作流水：每次下 CLOSE 单记一行（决策做了什么/实际下了什么）。
CREATE TABLE IF NOT EXISTS five_min_exit_action (
    id          BIGSERIAL PRIMARY KEY,
    plan_id     BIGINT      NOT NULL REFERENCES five_min_exit_plan(id) ON DELETE CASCADE,
    kind        TEXT        NOT NULL CHECK (kind IN ('HARD_STOP','TP')),
    lots        INTEGER     NOT NULL CHECK (lots > 0),
    price_ref   NUMERIC     NOT NULL,
    order_id    BIGINT,     -- 实际平仓单 id
    state       TEXT        NOT NULL,   -- ACK / DUPLICATE_BLOCKED / REJECTED ...
    note        TEXT,
    fired_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_five_min_exit_action_plan ON five_min_exit_action (plan_id);

-- 独立开关：默认 false，与 five_min_sim_scan_enabled 完全独立。
INSERT INTO cfg_feature_switch
    (switch_key, switch_name, switch_group, enabled, value, description)
VALUES
    (
        'five_min_exit_scan_enabled',
        '5m 模拟盘离场扫描（sim，零真实资金）',
        'strategy',
        false,
        '',
        'CL-20261124-06。false=离场作业整轮跳过（默认，现状逐位不变）；'
        || 'true=每 :05/:35 轮询 five_min_exit_plan 的 OPEN 计划，按 5M-EXIT-CALIBER '
        || '决策后经 /execution/sim/orders 下 CLOSE 单。'
        || '强制 broker=sim；实盘开关 five_min_pool_position_enabled 保持 off。'
    )
ON CONFLICT (switch_key) DO NOTHING;

-- 调度任务登记（expected_gap_days=3 与 five_min_scan 同，避免心跳误判）。
INSERT INTO ops.scheduled_task
    (task_id, name, purpose, executor, schedule, schedule_desc, target, location,
     enabled, owner, tags, expected_gap_days, meta)
VALUES
    ('five_min_exit_scan',
     '5m 模拟盘离场扫描（sim）',
     '每 :05/:35 轮询 five_min_exit_plan OPEN 计划，exit_guard 决策后下 CLOSE 单；'
     || '独立开关 five_min_exit_scan_enabled 控制，默认关闭',
     'apscheduler',
     '5,35 * * * *',
     '每整点后 :05/:35（与 five_min_scan 同节奏）',
     'app.scheduler:_five_min_exit_job',
     'cloud',
     true,
     'qhyc',
     ARRAY['5m', 'strategy', 'sim', 'exit', 'calibration'],
     3,
     ('{"switch_key": "five_min_exit_scan_enabled", '
     || '"broker_requirement": "sim", '
     || '"execution_enabled_independent": true, '
     || '"source_of_truth": "app/scheduler.py::_five_min_exit_job"}')::jsonb
    )
ON CONFLICT (task_id) DO NOTHING;

COMMIT;
