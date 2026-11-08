-- =====================================================================
-- 057: 启动参数快照 + 策略信号流水 params_hash（P2-2 / CL-20261108-10）
-- ---------------------------------------------------------------------
-- 解决什么问题
--   P2-1（CL-20261108-07）已经能逐项打印「此刻实际生效」的 fusion 参数并
--   算出 signal_params_hash，但它只回答「现在」，回答不了：
--     · 昨天 15:00 那次进程启动跑的是哪套参数？
--     · 这条已经落库的信号，是用哪套参数算出来的？
--   审计报告 §4④ 的「回测报告的参数 vs 生产在跑的参数可能不是同一套」，
--   缺的正是这条可追溯链。
--
-- 与 P2-1 断言的分工
--   OPS-FUSION-PROFILE-PINNED 管「参数不许静默漂移」（钉住 V3.4 基线）；
--   OPS-CONFIG-SNAPSHOT-FRESH 管「快照/信号必须绑定当前生效参数」。
--   两者互补，不重复。
--
-- ⚠ 为什么不给 public.backtest_result 加列
--   它是 **LSTM 预测模型**回测结果（run_id / symbol / model / window_len /
--   dir_acc / mae / rmse / by_state），参数口径是 BacktestParams，与 fusion
--   策略参数无关。挂同一个 params_hash 会制造新的错配，比不加更糟。
--   策略回测（app/backtest/fusion_backtest.py）目前不落库，故本迁移只贯穿到
--   **策略信号流水** fusion_signal_log + 启动账本 ops.config_snapshot。
--
-- ⚠ 发布顺序（CL-20261108-10 的强制约束）
--   本文件必须先单独发布并应用，再发布 app/ 接线代码。原因：
--   app/ 变更会触发 publish.py 双重启；若 ORM 已带 params_hash 列而数据库
--   尚未 ALTER，persist_signals 的 INSERT 会整批失败（旁路只告警 → 静默丢信号）。
--   先加列（DEFAULT NULL，旧进程不指定该列，零影响）再重启，才安全。
-- =====================================================================

BEGIN;

-- ---------- 1) 启动快照账本 ----------
CREATE TABLE IF NOT EXISTS ops.config_snapshot (
    id                  bigserial PRIMARY KEY,
    captured_at         timestamptz NOT NULL DEFAULT now(),
    role                text NOT NULL,              -- api / scheduler / manual
    app_env             text NOT NULL,
    git_head            text,
    config_sha1         text,
    signal_params_hash  text NOT NULL,
    full_params_hash    text NOT NULL,
    profile_label       text NOT NULL,
    effective           jsonb NOT NULL,             -- 42 字段逐项 {value,default,source,group}
    layers              jsonb NOT NULL,             -- L1~L5 覆盖链实际状态
    warnings            jsonb,
    python              text,
    pid                 integer,
    host                text
);

CREATE INDEX IF NOT EXISTS idx_config_snapshot_captured
    ON ops.config_snapshot (captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_config_snapshot_role_captured
    ON ops.config_snapshot (role, captured_at DESC);

COMMENT ON TABLE ops.config_snapshot IS
    '进程启动时的实际生效 fusion 参数快照（CL-20261108-10）。每次 api/scheduler 启动写一行；'
    'signal_params_hash 与 scripts/ops/dump_fusion_profile.py 同源，供「某次运行用的是哪套参数」追溯。';

-- ---------- 2) 策略信号流水贯穿 ----------
-- public.fusion_signal_log 实测 relkind='r'（真表，不是分层 shim 视图），可直接 ALTER。
-- 列允许 NULL：迁移已应用但进程仍是旧代码时不指定该列，零影响；
-- 新进程启动后由 persist_signals 填入 current_params_hash()。
ALTER TABLE public.fusion_signal_log
    ADD COLUMN IF NOT EXISTS params_hash text;

COMMENT ON COLUMN public.fusion_signal_log.params_hash IS
    '产生该信号时实际生效的 fusion signal_params_hash（CL-20261108-10 / 057）。'
    'NULL = 057 之前的旧行或 hash 计算失败，守卫只校验非 NULL 值的一致性。';

COMMIT;
