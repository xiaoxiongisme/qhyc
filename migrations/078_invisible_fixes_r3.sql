-- CL-20261112-06：第三轮「看不见的问题」修复所需的库侧对象。
--
-- 背景（2026-11-12 第三轮审计实证）：
--   * migration 077 的 AFTER UPDATE 触发器是死机制：常态写入全是
--     ON CONFLICT (symbol,ts) DO NOTHING / COPY，从不产生 UPDATE；
--     ops.minute_bar_src_overlay 实测 0 行，而断言只验「挂载」就 PASS。
--   * 真实风险是「同一 (symbol,ts) 跨源冲突时后者被静默丢弃」，必须在
--     INSERT 冲突处留痕 —— 触发器挂 BEFORE INSERT，覆盖全部写入路径。
--   * cron_invariant_guard 半途崩溃无完成标记；alerting 的日配额是进程内
--     dict（重启归零）且只统计单函数，挡不住多来源共用 pushplus 额度。
--
-- 本迁移只建对象，不改任何业务数据。可重复执行（IF NOT EXISTS / OR REPLACE）。
BEGIN;

-- ---------------------------------------------------------------------------
-- 1) L0 跨源冲突留痕表 + BEFORE INSERT 触发器
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops.l0_src_conflict (
    symbol        text        NOT NULL,
    ts            timestamptz NOT NULL,
    attempted_src text        NOT NULL DEFAULT '',
    kept_src      text        NOT NULL DEFAULT '',
    layer         text        NOT NULL DEFAULT '',
    first_seen    timestamptz NOT NULL DEFAULT now(),
    last_seen     timestamptz NOT NULL DEFAULT now(),
    n_attempts    bigint      NOT NULL DEFAULT 1,
    PRIMARY KEY (symbol, ts, attempted_src, kept_src)
);
CREATE INDEX IF NOT EXISTS ix_l0_src_conflict_last_seen
    ON ops.l0_src_conflict (last_seen);

COMMENT ON TABLE ops.l0_src_conflict IS
    'CL-20261112-06：L0 主键 (symbol,ts) 跨源冲突留痕。DO NOTHING 丢掉的第二源在此可见；'
    '同一冲突键只留一行并累计次数，不会因每轮重复采集刷表。';

CREATE OR REPLACE FUNCTION ops.fn_log_l0_src_conflict()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    kept text;
BEGIN
    -- TG_RELID 是当前 chunk（或探针 TEMP 表）；按主键精确查已存在的行。
    -- 只在「已存在且 src 不同」时留痕：同 src 的常规幂等重跑零写入。
    EXECUTE format('SELECT src FROM %s WHERE symbol = $1 AND ts = $2',
                   TG_RELID::regclass)
       INTO kept USING NEW.symbol, NEW.ts;
    IF kept IS NOT NULL AND kept IS DISTINCT FROM NEW.src THEN
        INSERT INTO ops.l0_src_conflict
            (symbol, ts, attempted_src, kept_src, layer)
        VALUES (NEW.symbol, NEW.ts,
                coalesce(NEW.src, ''), coalesce(kept, ''),
                coalesce(NEW.layer, ''))
        ON CONFLICT (symbol, ts, attempted_src, kept_src) DO UPDATE
            SET last_seen = EXCLUDED.last_seen,
                n_attempts = ops.l0_src_conflict.n_attempts + 1;
    END IF;
    RETURN NEW;                    -- 不改变写入语义：冲突仍由 DO NOTHING 吞掉
EXCEPTION WHEN OTHERS THEN
    -- fail-open：审计设施故障绝不许阻断行情采集
    RETURN NEW;
END $$;

COMMENT ON FUNCTION ops.fn_log_l0_src_conflict() IS
    'CL-20261112-06：BEFORE INSERT 行级触发器，跨源同 (symbol,ts) 冲突留痕。'
    '由 077 的 AFTER UPDATE 触发器补充「事后改写 src」场景。';

DROP TRIGGER IF EXISTS trg_l0_src_conflict ON l0_raw.minute_bar;
CREATE TRIGGER trg_l0_src_conflict
    BEFORE INSERT ON l0_raw.minute_bar
    FOR EACH ROW EXECUTE FUNCTION ops.fn_log_l0_src_conflict();

-- ---------------------------------------------------------------------------
-- 2) 不变量守卫「轮次完成标记」：只有跑完整轮才写这行
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops.invariant_run (
    run_id      text        PRIMARY KEY,
    finished_at timestamptz NOT NULL DEFAULT now(),
    n_total     integer     NOT NULL,
    n_fail      integer     NOT NULL DEFAULT 0,
    n_waived    integer     NOT NULL DEFAULT 0,
    n_warn      integer     NOT NULL DEFAULT 0
);

COMMENT ON TABLE ops.invariant_run IS
    'CL-20261112-06：守卫 --record 完整跑完后写的完成标记。'
    'ops.invariant_check 是逐条流水，「写入 60 条后崩溃」看起来也像跑过；'
    '本表缺 run_id = 该轮未跑完，供 _check_guard_liveness 判定。';

-- 历史回填只用于建立基线（让今天 09:10 那轮不因新机制缺失被误判），
-- 不代表对历史轮次做过恢复认定；从本迁移起由 guard_invariants.py 真写入。
INSERT INTO ops.invariant_run (run_id, finished_at, n_total, n_fail, n_waived)
SELECT run_id, max(checked_at), count(*)::int,
       count(*) FILTER (WHERE upper(status) = 'FAIL')::int,
       count(*) FILTER (WHERE upper(status) = 'WAIVED')::int
FROM ops.invariant_check
WHERE checked_at > now() - interval '7 days'
  AND run_id IS NOT NULL
GROUP BY run_id
ON CONFLICT (run_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 3) 当前状态视图：每个 check_key 只取最新一轮结果
--    （skill 铁律：invariant_check 是事件表，不能拿来当状态表读）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW ops.v_invariant_current AS
SELECT DISTINCT ON (check_key)
       check_key, title, severity, status, expected, observed, detail,
       checked_at, run_id, waiver_ref
FROM ops.invariant_check
ORDER BY check_key, checked_at DESC, id DESC;

COMMENT ON VIEW ops.v_invariant_current IS
    'CL-20261112-06：不变量断言的当前状态（每 key 取最新一轮），'
    '替代直接读 ops.invariant_check 流水判断「现在有没有 FAIL」。';

-- ---------------------------------------------------------------------------
-- 4) 推送额度持久化 + 全来源发送留痕
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops.alert_push_quota (
    day         date        PRIMARY KEY,
    used        integer     NOT NULL DEFAULT 0,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ops.push_send_ledger (
    id          bigserial   PRIMARY KEY,
    sent_at     timestamptz NOT NULL DEFAULT now(),
    source      text        NOT NULL DEFAULT 'unknown',
    channel     text        NOT NULL DEFAULT '',
    ok          boolean     NOT NULL DEFAULT false,
    title       text
);
CREATE INDEX IF NOT EXISTS ix_push_send_ledger_sent_at
    ON ops.push_send_ledger (sent_at);

COMMENT ON TABLE ops.alert_push_quota IS
    'CL-20261112-06：告警日配额 DB 原子计数，替代重启即归零的进程内 dict。';
COMMENT ON TABLE ops.push_send_ledger IS
    'CL-20261112-06：所有 send_notify 调用的发送留痕（含交易信号），'
    '让「多来源共用 pushplus 服务端额度」可见、可统计。';

COMMIT;
