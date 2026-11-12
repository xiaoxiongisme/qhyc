-- =============================================================================
-- migrations/079_l0_reconcile.sql  (CL-20261112-09)
--
-- 实时链路裁切 + 跨源占位跳过 + 日终全天校验补缺 的配套审计表与作业登记。
--
-- 背景：akshare period=1 固定返回约 1024 根历史分钟，实时任务每轮重放整段窗口，
-- 撞上先到的历史源占住的 (symbol, ts) 键 → 078 触发器捕获 309+28 行冲突并每轮
-- 刷 last_seen。改造后实时只提交最近 120 根并显式跳过跨源占位键，长缺口由
-- 15:20 日终作业用完整窗口补齐。审计表让守卫能验证「护栏真在跑」。
--
-- ⚠ 本迁移只建表 + 登记作业字典，**不改任何行情数据、不改覆盖语义**。
--    已有 ops.l0_src_conflict 的 337 行历史留痕保留，作为改造前基线证据。
-- =============================================================================

CREATE TABLE IF NOT EXISTS ops.l0_ingest_audit (
    id                  bigserial PRIMARY KEY,
    run_at              timestamptz NOT NULL DEFAULT now(),
    chain               text        NOT NULL,           -- continuous / contract
    n_symbols           integer     NOT NULL DEFAULT 0,
    n_candidates        integer     NOT NULL DEFAULT 0, -- 过滤前候选行数
    n_written           integer     NOT NULL DEFAULT 0, -- 实际提交行数
    n_skipped_cross_src integer     NOT NULL DEFAULT 0, -- 被其它 src 占据而跳过
    max_lookback_bars   integer     NOT NULL DEFAULT 0, -- 单品种最大候选根数
    min_ts              timestamptz,
    max_ts              timestamptz,
    extra               jsonb       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ix_l0_ingest_audit_chain_run
    ON ops.l0_ingest_audit (chain, run_at DESC);

COMMENT ON TABLE ops.l0_ingest_audit IS
    'CL-20261112-09 每轮采集护栏审计；OPS-L0-REPLAY-BOUNDED / OPS-L0-CROSS-SRC-QUIET 据此判定';

CREATE TABLE IF NOT EXISTS ops.l0_eod_check (
    id           bigserial PRIMARY KEY,
    trade_date   date        NOT NULL,
    symbol       text        NOT NULL,
    window_start timestamptz NOT NULL,
    window_end   timestamptz NOT NULL,
    n_expected   integer     NOT NULL DEFAULT 0,  -- 接口在该交易日的分钟数
    n_existing   integer     NOT NULL DEFAULT 0,  -- 库里已有键数（含任意 src）
    n_missing    integer     NOT NULL DEFAULT 0,  -- 补缺**复查后**仍缺（守卫直接判 0）
    n_inserted   integer     NOT NULL DEFAULT 0,  -- 本轮补进去的
    n_cross_src  integer     NOT NULL DEFAULT 0,  -- 键被其它 src 占据（只报告）
    n_anomaly    integer     NOT NULL DEFAULT 0,  -- 零量/非正价/high<low（只报告）
    status       text        NOT NULL DEFAULT 'ok', -- ok / repaired / no_source
    checked_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT l0_eod_check_unique UNIQUE (trade_date, symbol)
);

CREATE INDEX IF NOT EXISTS ix_l0_eod_check_trade_date
    ON ops.l0_eod_check (trade_date DESC);

COMMENT ON TABLE ops.l0_eod_check IS
    'CL-20261112-09 15:20 日终全天校验结果；DATA-L0-EOD-RECONCILE 据此判定，不依赖作业流水 success';

-- 改造上线基线：把 078 触发器已捕获的 337 行历史留痕钉住。
-- OPS-L0-CROSS-SRC-QUIET 断言「此后不得再新增」，避免靠时间窗口衰减的假绿
-- （窗口太宽要等 N 小时才 PASS，太窄又会漏掉采集间隔）。
CREATE TABLE IF NOT EXISTS ops.l0_guard_baseline (
    key       text PRIMARY KEY,
    value_ts  timestamptz,
    value_num numeric,
    note      text
);

INSERT INTO ops.l0_guard_baseline (key, value_ts, value_num, note)
SELECT 'conflict_last_seen',
       max(last_seen),
       sum(n_attempts),
       'CL-20261112-09 上线基线：337 行历史留痕保留为改造前证据，之后新增即回归'
FROM ops.l0_src_conflict
ON CONFLICT (key) DO NOTHING;

-- 作业字典：executor=apscheduler 的行会被 scheduler_resync 用 schedule 覆盖代码
-- CronTrigger（频率单一真相）。星期位用 `*`：CronTrigger.from_crontab 的星期
-- 语义与标准 crontab 差一天（skill 坑 #1），写 0-4 会变成周二~周六；
-- 周末由 l0_eod_reconcile.trading_session_bounds 自行跳过。
INSERT INTO ops.scheduled_task
    (task_id, name, purpose, executor, schedule, schedule_desc, target,
     location, enabled, expected_gap_days, tags)
VALUES
    ('l0_eod_reconcile',
     'L0 全天分钟校验补缺',
     '每交易日 15:20 用 akshare 完整窗口校验上一工作日夜盘~当日日盘的 continuous 分钟，只 INSERT 缺失键（keep-first，不覆盖任何已有 src）；异常值只报告告警。补偿实时链路裁成 120 根后失去的长缺口自愈能力。CL-20261112-09',
     'apscheduler',
     '20 15 * * *',
     '每日 15:20（周末作业内跳过）',
     'app/scheduler.py::_l0_eod_reconcile_job',
     'cloud-container',
     true,
     1,
     ARRAY['l0', 'data', 'reconcile']::text[])
ON CONFLICT (task_id) DO NOTHING;
