-- =====================================================================
-- 052: 改造台账（Change Ledger）+ 不变量守卫结果表
-- ---------------------------------------------------------------------
-- 解决什么问题
--   2026-11-07 审计暴露一类系统性风险：改造「讨论过、部分落地、然后搁置」，
--   系统里没有任何地方记录它停在哪一步。实例：
--     · 数据层白名单方案 v3 已把 l3_ref.tradable_variety 落库（38 行），
--       但下游过滤（L1/L2/信号池）从未实施 → 白名单 38 / 配置 50 / L1 81 /
--       信号 41 四套口径并存，只能靠人肉发现；
--     · portfolio_equity 建表后写入方从未实现（0 行），熔断恒放行。
--   本表把「改造」变成一等公民：每条改造有 ID、状态、影响面、验收断言、
--   回滚方式。任何未完成的改造必须显式挂账，不得处于「没人知道」状态。
--
-- 与不变量守卫的关系
--   ops.change_log.invariant_keys 声明「这条改造改变了哪些不变量」。
--   scripts/ops/guard_invariants.py 若发现某不变量不成立，但存在
--   status ∈ (pending/approved/in_progress/deferred) 的台账项声明了该 key，
--   则降级为 WAIVED（已知缺口，附台账 ID）而不是 FAIL（背约）。
--   → 台账项一旦改为 implemented，豁免自动失效，不变量必须真通过。
--     这就是「不许假装通过、也不许每天刷红」的机制。
-- =====================================================================

-- ---------- 1) 改造台账 ----------
CREATE TABLE IF NOT EXISTS ops.change_log (
    change_id       text PRIMARY KEY,              -- CL-YYYYMMDD-NN
    title           text NOT NULL,
    category        text NOT NULL,                 -- code/data/config/param/schema/ops/research
    status          text NOT NULL,                 -- proposed/approved/in_progress/deferred/implemented/rolled_back/rejected
    risk            text NOT NULL DEFAULT 'medium',-- low/medium/high
    -- 为什么要做（问题陈述，必须可追溯到一个实证或一个决策）
    rationale       text,
    -- 改了什么（实施后回填）
    change_summary  text,
    -- 影响面：表/视图/服务/品种口径/参数
    impact_scope    text,
    -- 验收断言：逗号分隔的不变量 key，见 guard_invariants.INVARIANTS
    invariant_keys  text,
    -- 未采纳/搁置原因（deferred 必填），以及拍板人
    decision_note   text,
    -- 实施证据
    commit_sha      text,
    migration_file  text,
    verified_at     timestamptz,
    -- 回滚方式（可执行描述，不含"见文档"这种空话）
    rollback        text,
    -- 关联归档/报告（相对或绝对路径）
    archive_ref     text,
    items           jsonb,                         -- 子决策点，如 {"D1":"A","D7":"待定"}
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_change_status CHECK (status IN (
        'proposed','approved','in_progress','deferred','implemented','rolled_back','rejected')),
    CONSTRAINT ck_change_category CHECK (category IN (
        'code','data','config','param','schema','ops','research')),
    CONSTRAINT ck_change_risk CHECK (risk IN ('low','medium','high'))
);

COMMENT ON TABLE  ops.change_log IS
    '改造台账：qhyc 每一次结构性改造的唯一真相。未完成改造必须显式挂账，禁止"半实施无人知"。';
COMMENT ON COLUMN ops.change_log.status IS
    'proposed=提案 approved=已拍板待做 in_progress=进行中 deferred=暂缓(必填decision_note) implemented=已实施(需commit_sha+verified_at) rolled_back=已回滚 rejected=否决';
COMMENT ON COLUMN ops.change_log.invariant_keys IS
    '逗号分隔的 guard_invariants.INVARIANTS key；未完成状态期间这些不变量失败会降级为 WAIVED';
COMMENT ON COLUMN ops.change_log.items IS
    '子决策点拍板结果，如 {"D1":"A 只导出不删","D7":"待定"}';

CREATE INDEX IF NOT EXISTS ix_change_log_status   ON ops.change_log (status);
CREATE INDEX IF NOT EXISTS ix_change_log_created  ON ops.change_log (created_at DESC);

-- 状态机约束：implemented 必须有 commit 或 migration 证据 + 验收时间
ALTER TABLE ops.change_log DROP CONSTRAINT IF EXISTS ck_change_implemented_evidence;
ALTER TABLE ops.change_log ADD CONSTRAINT ck_change_implemented_evidence CHECK (
    status <> 'implemented'
    OR ((commit_sha IS NOT NULL OR migration_file IS NOT NULL) AND verified_at IS NOT NULL)
);
-- deferred/in_progress 必须写明为什么没做完
ALTER TABLE ops.change_log DROP CONSTRAINT IF EXISTS ck_change_deferred_reason;
ALTER TABLE ops.change_log ADD CONSTRAINT ck_change_deferred_reason CHECK (
    status NOT IN ('deferred','rejected') OR (decision_note IS NOT NULL AND length(decision_note) > 0)
);

-- ---------- 2) 不变量守卫结果（每次运行留痕，供趋势与告警节流） ----------
CREATE TABLE IF NOT EXISTS ops.invariant_check (
    id           bigserial PRIMARY KEY,
    run_id       text NOT NULL,          -- 一次完整扫描共用一个 run_id
    check_key    text NOT NULL,
    title        text NOT NULL,
    severity     text NOT NULL,          -- block/warn/info
    status       text NOT NULL,          -- PASS/FAIL/WAIVED/SKIP
    expected     text,
    observed     text,
    detail       text,
    waiver_ref   text,                   -- 命中豁免时的 change_id
    checked_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_inv_status CHECK (status IN ('PASS','FAIL','WAIVED','SKIP')),
    CONSTRAINT ck_inv_severity CHECK (severity IN ('block','warn','info'))
);

CREATE INDEX IF NOT EXISTS ix_invariant_check_run ON ops.invariant_check (run_id);
CREATE INDEX IF NOT EXISTS ix_invariant_check_key ON ops.invariant_check (check_key, checked_at DESC);

COMMENT ON TABLE ops.invariant_check IS
    '不变量守卫逐条结果。同一 run_id 为一次完整扫描；FAIL 会推送告警，WAIVED 只在日报汇总。';

-- ---------- 3) 视图：当前未完成改造（一眼看到"欠债"） ----------
CREATE OR REPLACE VIEW ops.v_open_changes AS
SELECT change_id, title, category, status, risk,
       coalesce(invariant_keys, '') AS invariant_keys,
       coalesce(decision_note, '')  AS decision_note,
       created_at,
       (CURRENT_DATE - created_at::date) AS age_days
FROM ops.change_log
WHERE status IN ('proposed','approved','in_progress','deferred')
ORDER BY
    CASE risk WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
    created_at;

COMMENT ON VIEW ops.v_open_changes IS
    '当前未完成/暂缓的改造清单，按风险降序。guard_invariants 的豁免来源即此表。';
