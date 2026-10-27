-- =============================================================
-- 044_risk_gate.sql
-- 交易外壳三层：P0 盘前作战门 / P1 离场自动化 / P2 品种减法
-- -------------------------------------------------------------
-- 背景（2026-10-27 事故与账单复盘，均为真实数据）：
--   * 2026-10-26 夜盘：甲醇涨停触发交易所临时提保，账户保证金占用率达 110%。
--     复盘确认根因**不是单边重仓**，而是「多空双向锁仓按双边全额收取保证金」
--     （MA701 多4空4 + EG2701 多2空2，毛名义 46.3 万，净敞口为 0 但保证金照收两份）。
--     原保证金合计 ≈ 4.1 万；甲醇 8%→15% 提保后 ≈ 5.9 万 → /权益 5.4 万 ≈ 110%。
--   * 726 笔真实往返（5 账户，2023-08-23~2026-10-27，以「平仓事件」为一条往返）：
--     净 −121,456，胜率 73.4%，R:R 0.24，期望 −167 元/笔；
--     赢单持仓中位 0.37h vs 亏单 2.96h（7.9 倍）；隔夜单净 −100,273 = 全部净亏的 83%；
--     持仓 3 天以上 29 笔合计 −84,379（占全部净亏 69%）。
--
-- 设计原则（沿用本项目纪律）：
--   1) 全部为**附加表**，不改任何既有表；门禁默认 enabled=False 时与"从未上线"逐位等价
--   2) 保证金与手数一律 NUMERIC，不用 float（与 execution_order 口径一致）
--   3) **绝不静默**：数据缺失时 verdict 只能是 UNKNOWN，禁止退化为 GREEN
--   4) 幂等可重复执行（IF NOT EXISTS / 索引 IF NOT EXISTS）
-- =============================================================

BEGIN;

-- ------------------------------------------------------------
-- 1. 账户权益（盘前门的唯一输入缺口）
-- ------------------------------------------------------------
-- 为什么单独立表：qhyc 至今没有账户实时权益（PRD_P0-2 §3 已标注缺失），
-- 而「保证金占用率」的分母就是它。没有它，任何仓位上限都是空谈。
-- 来源三档：BROKER（柜台/无限易查询） > MANUAL（手工补录） > SETTLE（结算单导入）
CREATE TABLE IF NOT EXISTS account_equity (
    trade_date    DATE          NOT NULL,
    account       TEXT          NOT NULL,
    equity        NUMERIC(20,2) NOT NULL CHECK (equity > 0),   -- 动态权益（含浮动盈亏）
    available     NUMERIC(20,2),                                -- 可用资金
    margin_used   NUMERIC(20,2),                                -- 已占用保证金（柜台口径优先）
    source        TEXT          NOT NULL DEFAULT 'MANUAL'
                  CHECK (source IN ('BROKER','MANUAL','SETTLE')),
    note          TEXT,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, account)
);

CREATE INDEX IF NOT EXISTS ix_account_equity_acct
    ON account_equity (account, trade_date DESC);

COMMENT ON TABLE  account_equity IS '账户权益逐日快照：盘前门计算保证金占用率的分母（qhyc 此前缺失）';
COMMENT ON COLUMN account_equity.equity IS '动态权益（含浮盈浮亏），禁止填「静态权益」——那会系统性低估占用率';

-- ------------------------------------------------------------
-- 2. 盘前作战门快照（P0）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS preopen_snapshot (
    id                    BIGSERIAL PRIMARY KEY,
    trade_date            DATE        NOT NULL,
    session               TEXT        NOT NULL,   -- day / night
    account               TEXT        NOT NULL,
    -- 判定结果：GREEN=可开新仓 / YELLOW=只减不增 / RED=禁止开新仓 / UNKNOWN=数据缺失
    verdict               TEXT        NOT NULL
                          CHECK (verdict IN ('GREEN','YELLOW','RED','UNKNOWN')),
    margin_usage_now      NUMERIC(8,4),   -- 当前占用率（小数，0.55 = 55%）
    margin_usage_extreme  NUMERIC(8,4),   -- 极端情景占用率（模拟涨停+再提保）
    gross_notional        NUMERIC(20,2),  -- 毛名义（多空分别计，锁仓不轧差）
    net_notional          NUMERIC(20,2),  -- 净名义（诊断用，绝不用于保证金）
    equity                NUMERIC(20,2),
    locked_symbols        TEXT[],         -- 存在双向持仓（锁仓）的合约
    checks                JSONB       NOT NULL,   -- 逐项检查结果（含阈值与实测值）
    reasons               TEXT[],         -- 命中的拦截理由（人话）
    actions               TEXT[],         -- 建议动作
    markdown              TEXT,           -- 推送正文（留痕，便于回查"当时到底提示了什么"）
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (trade_date, session, account)
);

CREATE INDEX IF NOT EXISTS ix_preopen_date
    ON preopen_snapshot (trade_date DESC, account);

COMMENT ON TABLE  preopen_snapshot IS 'P0 盘前作战门逐次快照：三色灯 + 逐项检查证据 + 推送正文留痕';
COMMENT ON COLUMN preopen_snapshot.gross_notional IS '毛名义 = Σ(多头手数×乘数×价) + Σ(空头手数×乘数×价)；锁仓时保证金按此双份收取';
COMMENT ON COLUMN preopen_snapshot.margin_usage_extreme IS '极端情景 = 按涨停价重算名义 × 保证金率 × 再提保倍数；这是「今晚会不会被追保」的唯一可靠答案';

-- ------------------------------------------------------------
-- 3. 离场计划（P1）
-- ------------------------------------------------------------
-- 核心思想：把「卖不卖」这一个 100% 情绪决策，拆成 3 个机械触发点。
-- R 的定义：R = |入场价 − 初始止损| × 乘数 × 手数（该笔的初始风险金额）。
CREATE TABLE IF NOT EXISTS exit_plan (
    id              BIGSERIAL PRIMARY KEY,
    account         TEXT        NOT NULL,
    real_symbol     TEXT        NOT NULL,
    direction       TEXT        NOT NULL CHECK (direction IN ('LONG','SHORT')),
    lots_open       INTEGER     NOT NULL CHECK (lots_open > 0),
    entry_px        NUMERIC     NOT NULL,
    init_stop       NUMERIC     NOT NULL,
    r_unit          NUMERIC     NOT NULL CHECK (r_unit > 0),  -- 每手 1R 的价格距离
    atr             NUMERIC,
    -- 分批止盈阶梯（1R 出 1/3、2R 出 1/3、余量跟吊灯）
    tp1_px          NUMERIC,
    tp1_lots        INTEGER,
    tp2_px          NUMERIC,
    tp2_lots        INTEGER,
    tp1_done_at     TIMESTAMPTZ,
    tp2_done_at     TIMESTAMPTZ,
    -- 时间止损：到点仍未达 1R 即减半（治「扛」）
    time_stop_at    TIMESTAMPTZ,
    time_stop_done  BOOLEAN     NOT NULL DEFAULT FALSE,
    -- 吊灯止损（余量用）
    trail_stop      NUMERIC,
    status          TEXT        NOT NULL DEFAULT 'OPEN'
                    CHECK (status IN ('OPEN','CLOSED','CANCELED')),
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account, real_symbol, direction)
);

CREATE INDEX IF NOT EXISTS ix_exit_plan_open
    ON exit_plan (account, status) WHERE status = 'OPEN';

COMMENT ON TABLE exit_plan IS 'P1 离场计划：分批止盈 + 时间止损 + 吊灯；一笔持仓一行，状态机 OPEN/CLOSED/CANCELED';
COMMENT ON COLUMN exit_plan.r_unit IS '每手 1R 的价格距离 = |入场−初始止损|；止盈位与时间止损判据均以 R 为单位，不用百分比';

-- 离场动作流水（每次生成减仓单记一行，便于事后核对"计划做了什么/实际做了什么"）
CREATE TABLE IF NOT EXISTS exit_action (
    id          BIGSERIAL PRIMARY KEY,
    plan_id     BIGINT      NOT NULL REFERENCES exit_plan (id) ON DELETE CASCADE,
    fired_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind        TEXT        NOT NULL
                CHECK (kind IN ('TP1','TP2','TIME_STOP','TRAIL','HARD_STOP','MANUAL')),
    lots        INTEGER     NOT NULL CHECK (lots > 0),
    price_ref   NUMERIC,
    order_id    BIGINT,
    note        TEXT
);

CREATE INDEX IF NOT EXISTS ix_exit_action_plan ON exit_action (plan_id, fired_at DESC);

COMMENT ON TABLE exit_action IS '离场动作流水：TP1/TP2/TIME_STOP/TRAIL/HARD_STOP/MANUAL 六类，供「计划 vs 实际」对拍';

-- ------------------------------------------------------------
-- 4. 交易流水（P1 连亏熔断 + P2 接受标准的计算底座）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trade_journal (
    id             BIGSERIAL PRIMARY KEY,
    account        TEXT        NOT NULL,
    real_symbol    TEXT        NOT NULL,
    direction      TEXT        CHECK (direction IN ('LONG','SHORT')),
    open_at        TIMESTAMPTZ NOT NULL,
    close_at       TIMESTAMPTZ NOT NULL,
    lots           INTEGER     NOT NULL CHECK (lots > 0),
    open_px        NUMERIC     NOT NULL,
    close_px       NUMERIC     NOT NULL,
    gross          NUMERIC(20,2) NOT NULL,
    fee            NUMERIC(20,2) NOT NULL DEFAULT 0,
    net            NUMERIC(20,2) NOT NULL,
    planned_stop   NUMERIC,            -- 计划止损价（无计划则为 NULL）
    r_multiple     NUMERIC(10,4),      -- 实际盈亏 / 计划风险；无计划止损时为 NULL
    close_reason   TEXT,               -- STOP_LOSS / TAKE_PROFIT / TIME_STOP / SIGNAL / MANUAL
    violated       BOOLEAN     NOT NULL DEFAULT FALSE,  -- 是否违背计划（扛单/提前平）
    source         TEXT        NOT NULL DEFAULT 'IMPORT'
                   CHECK (source IN ('IMPORT','RUNTIME','MANUAL')),
    imported_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 幂等：同一账户同一合约同一开平时刻同一手数只入一次
    UNIQUE (account, real_symbol, open_at, close_at, lots)
);

CREATE INDEX IF NOT EXISTS ix_trade_journal_close
    ON trade_journal (account, close_at DESC);
CREATE INDEX IF NOT EXISTS ix_trade_journal_sym
    ON trade_journal (real_symbol, close_at DESC);

COMMENT ON TABLE  trade_journal IS '平仓流水（往返级）：连亏熔断、R 倍数统计、剔除 TOP3 接受标准的统一数据源';
COMMENT ON COLUMN trade_journal.r_multiple IS '净盈亏 ÷ (每手计划风险×手数)。<−1 即「扛过止损」，是行为违规的量化证据';

-- ------------------------------------------------------------
-- 5. 人工干预记账（把「手动改止损」变成可审计的数字）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS manual_intervention (
    id            BIGSERIAL PRIMARY KEY,
    at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    account       TEXT        NOT NULL,
    real_symbol   TEXT        NOT NULL,
    kind          TEXT        NOT NULL
                  CHECK (kind IN ('MOVE_STOP','EARLY_EXIT','HOLD_PAST_STOP',
                                  'ADD_LOTS','REVERSE','SKIP_SIGNAL','OTHER')),
    planned_value NUMERIC,     -- 系统计划值（如计划止损价）
    actual_value  NUMERIC,     -- 实际执行值
    lots          INTEGER,
    note          TEXT,
    pnl_impact    NUMERIC(20,2),  -- 事后回填：这次干预相对「照系统做」的净差
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_manual_int_at ON manual_intervention (account, at DESC);

COMMENT ON TABLE manual_intervention IS '人工干预台账：每次手动偏离系统的动作记一行，月末汇总「干预净损益」——这是说服自己的唯一证据';

COMMIT;

-- =============================================================
-- 验收（应用后立即跑）
--   SELECT to_regclass('account_equity')      IS NOT NULL AS t1,
--          to_regclass('preopen_snapshot')    IS NOT NULL AS t2,
--          to_regclass('exit_plan')           IS NOT NULL AS t3,
--          to_regclass('exit_action')         IS NOT NULL AS t4,
--          to_regclass('trade_journal')       IS NOT NULL AS t5,
--          to_regclass('manual_intervention') IS NOT NULL AS t6;
--
-- 登记（务必登记，否则与 028/029 一样「应用了没登记」）：
--   INSERT INTO schema_migrations (filename, sha1, applied_at)
--   VALUES ('044_risk_gate.sql', '<sha1sum migrations/044_risk_gate.sql>', now())
--   ON CONFLICT (filename) DO NOTHING;
--
-- 回滚（仅在需要时手工执行）：
--   DROP TABLE IF EXISTS manual_intervention, trade_journal, exit_action,
--        exit_plan, preopen_snapshot, account_equity;
-- =============================================================
