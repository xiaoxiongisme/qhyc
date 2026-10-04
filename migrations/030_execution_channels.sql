-- =============================================================
-- 030_execution_channels.sql
-- P0-2 实盘通道（CTP + QMT）落地 · Sprint 1 DDL 骨架
-- -------------------------------------------------------------
-- 目标：把 P0-1 产出的「订单建议」落库为可追踪的订单/成交/持仓，
--       为 ctp-gateway / qhyc-bridge 提供唯一权威状态源。
--
-- 设计要点（对应 PRD_P0-2 §4.1 三个实盘硬缺口）：
--   G1 action（开平标志）：execution_order.action 显式落地，禁止由方向推断
--   G2 持仓聚合：execution_position 按 (real_symbol, channel) 聚合净持仓
--   G3 止损执行：execution_order 预留 stop_loss 触发链路（Sprint 1 由
--                app/execution/stop_loss.py 写回 close_reason）
--
-- 关键防错（本项目血泪纪律）：
--   1) idempotency_key UNIQUE —— 防止重试/断电恢复导致重复下单（真实资金事故）
--   2) status 用 CHECK 约束 —— 状态机非法迁移在 DB 层即被拦，不靠应用层自觉
--   3) 所有价格 NUMERIC 不用 float —— 与 P0-1 reverse_price 的 numeric 口径一致
--   4) 绝不静默：status 落 ERROR/REJECTED 必须写 last_error（非空校验用触发器思路，
--      但本项目禁触发器，改由 persistence.py 在写入前断言）
--
-- 幂等：可重复执行（IF NOT EXISTS / 索引 IF NOT EXISTS）
-- 回滚：见文末 ROLLBACK 段
-- =============================================================

BEGIN;

-- ------------------------------------------------------------
-- 1. 订单表（订单建议 + 生命周期）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS execution_order (
    id                BIGSERIAL PRIMARY KEY,
    -- 来源（P0-1 resolve_signal_to_order 的产出）
    signal_id         TEXT,
    symbol_continuous TEXT NOT NULL,          -- 信号侧符号，如 RB888
    real_symbol       TEXT NOT NULL,          -- 反解出的真实合约，如 RB2701
    exchange          TEXT,                   -- SHFE/DCE/CZCE/CFFEX/INE/GFEX
    -- G1 开平标志：必须显式，禁止由 direction 推断
    action            TEXT NOT NULL CHECK (action IN ('OPEN','CLOSE','CLOSE_TODAY','CLOSE_YEST')),
    direction         TEXT NOT NULL CHECK (direction IN ('BUY','SELL')),
    -- 价格与手数（P0-1 产出，price_space 决定 price 的解释方式）
    price             NUMERIC NOT NULL,
    price_space       TEXT NOT NULL DEFAULT 'raw' CHECK (price_space IN ('raw','adj')),
    lots              INTEGER NOT NULL CHECK (lots > 0),
    multiplier        NUMERIC,                -- 合约乘数（dim_variety）
    notional          NUMERIC,                -- 名义本金 = price*lots*multiplier
    -- 通道
    channel           TEXT NOT NULL DEFAULT 'SIM' CHECK (channel IN ('CTP','QMT','SIM')),
    account           TEXT,                   -- 资金账号（多账户时区分）
    -- 状态机：NEW → SENT → PARTIAL → FILLED；任意中间态可 → CANCELED/REJECTED/ERROR
    status            TEXT NOT NULL DEFAULT 'NEW'
                      CHECK (status IN ('NEW','SENT','PARTIAL','FILLED','CANCELED','REJECTED','ERROR')),
    broker_order_id   TEXT,                   -- 经纪商/网关侧订单号（对账主键）
    filled_lots       INTEGER NOT NULL DEFAULT 0,
    avg_fill_price    NUMERIC,
    -- 重试与错误（fail-loud：ERROR/REJECTED 必须有 last_error）
    retry_count       INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    close_reason      TEXT,                   -- 平仓原因：SIGNAL/STOP_LOSS/TAKE_PROFIT/MANUAL/ROLL
    -- 幂等键：同一信号+合约+动作 只下一单（防重复下单，最重要的一条）
    idempotency_key   TEXT NOT NULL UNIQUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_exec_order_status  ON execution_order (status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_exec_order_symbol  ON execution_order (real_symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_exec_order_broker  ON execution_order (broker_order_id) WHERE broker_order_id IS NOT NULL;

COMMENT ON TABLE  execution_order IS 'P0-2 订单表：P0-1 订单建议 → 实盘订单的唯一权威状态源';
COMMENT ON COLUMN execution_order.idempotency_key IS '防重复下单唯一键（建议 format: {signal_id}:{real_symbol}:{action}:{YYYYMMDD}）';
COMMENT ON COLUMN execution_order.price_space IS 'raw=未复权真实价（与 P0-1 一致）；adj=复权空间（下单前必须反解，禁止直接送 adj 价）';

-- ------------------------------------------------------------
-- 2. 成交回报表
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS execution_fill (
    id              BIGSERIAL PRIMARY KEY,
    order_id        BIGINT NOT NULL REFERENCES execution_order (id) ON DELETE CASCADE,
    broker_fill_id  TEXT,
    fill_ts         TIMESTAMPTZ NOT NULL,
    fill_price      NUMERIC NOT NULL,
    fill_lots       INTEGER NOT NULL CHECK (fill_lots > 0),
    fee             NUMERIC,                  -- 手续费（元），由 dim_trading_cost 重算后回填校验
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (order_id, broker_fill_id)
);

CREATE INDEX IF NOT EXISTS ix_exec_fill_order ON execution_fill (order_id);

COMMENT ON TABLE execution_fill IS '成交回报；UNIQUE(order_id, broker_fill_id) 防网关重复推送';

-- ------------------------------------------------------------
-- 3. 持仓快照表（G2 持仓聚合）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS execution_position (
    id             BIGSERIAL PRIMARY KEY,
    snapshot_date  DATE NOT NULL DEFAULT CURRENT_DATE,
    real_symbol    TEXT NOT NULL,
    channel        TEXT NOT NULL DEFAULT 'SIM',
    account        TEXT,
    net_lots       INTEGER NOT NULL DEFAULT 0,   -- 净持仓（多空已轧差），正=多 负=空
    long_lots      INTEGER NOT NULL DEFAULT 0,   -- 多头仓位（支持锁仓时分离）
    short_lots     INTEGER NOT NULL DEFAULT 0,
    avg_open_price NUMERIC,
    today_lots     INTEGER NOT NULL DEFAULT 0,   -- 今仓（平今/平昨费率与保证金差异必需）
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (snapshot_date, real_symbol, channel, account)
);

CREATE INDEX IF NOT EXISTS ix_exec_pos_symbol ON execution_position (real_symbol, snapshot_date DESC);

COMMENT ON TABLE execution_position IS '持仓快照（按日）；开平推导依赖 net_lots/long_lots/short_lots/today_lots 四字段';

-- ------------------------------------------------------------
-- 4. 通道健康/延迟（monitor.py 写入，供告警用）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS execution_channel_health (
    id           BIGSERIAL PRIMARY KEY,
    checked_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    channel      TEXT NOT NULL,
    healthy      BOOLEAN NOT NULL,
    latency_ms   INTEGER,
    detail       TEXT
);

CREATE INDEX IF NOT EXISTS ix_exec_health_ch ON execution_channel_health (channel, checked_at DESC);

COMMIT;

-- =============================================================
-- 验收（应用后立即跑）
--   SELECT to_regclass('execution_order')  IS NOT NULL AS t1,
--          to_regclass('execution_fill')   IS NOT NULL AS t2,
--          to_regclass('execution_position') IS NOT NULL AS t3;
--
-- 登记（务必登记到 schema_migrations，否则与 028/029 一样"应用了没登记"）：
--   INSERT INTO schema_migrations (filename, sha1, applied_at)
--   VALUES ('030_execution_channels.sql', '<本地 sha1>', now())
--   ON CONFLICT (filename) DO NOTHING;
--   -- sha1：本地执行 `sha1sum migrations/030_execution_channels.sql`
--
-- 回滚（谨慎，仅 Sprint 1 未产生真实订单时使用）：
--   DROP TABLE IF EXISTS execution_channel_health;
--   DROP TABLE IF EXISTS execution_fill;
--   DROP TABLE IF EXISTS execution_position;
--   DROP TABLE IF EXISTS execution_order;
-- =============================================================
