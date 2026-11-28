-- 090_five_min_s2_scan.sql
-- CL-20261128-02：S2 扫描入口接线——独立 five_min_s2_scan 作业 + 独立开关。
--
-- 背景：CL-20261128-01@718654d 已上线 S2_PRESET（EB 夜盘 hist_up010 动能做多
-- + 日线多 + 高波动门，SL3/TP4.5=1.5R，限价 off0.3/wait2），但生产扫描作业
-- _five_min_scan_job 仍传 DEFAULT_SYMBOLS + 默认 cfg（白盘两族），S2 样本无法
-- 积累，CL-20261122-01 的 P3 校准无法收口。
--
-- 【默认兼容 · 白盘链路逐位不动】
--   · five_min_sim_scan_enabled（白盘扫描，当前 t）不动；
--   · five_min_exit_sim_enabled（离场，当前 f）不动；
--   · 本迁移只新增一条独立开关 + 一条独立调度任务：
--       - five_min_s2_sim_enabled（默认 false）→ S2 开仓扫描作业整轮跳过；
--       - five_min_s2_scan → 每 :05/:35 复用 scan_once 扫 EB888，
--         传 cfg=S2_PRESET / symbols=S2_SYMBOLS / pool_id=5m-s2-sim，
--         意图经反解后由 /execution/sim/orders 落 SIM 单并写离场计划。
--   · S2 离场复用现有 _five_min_exit_job（全局扫 five_min_exit_plan，按
--     plan 的 pool_id=5m-s2-sim 隔离），不需另建离场链。
--   · S2 开仓与白盘开仓的 signal_id 前缀不同（5M-S2: vs 5M:），pool 不同，
--     互不撞幂等键、互不串仓。
--
-- 【命名纪律】开关名去掉 _enabled 后为 five_min_s2_sim，**不**等于任何真实
-- task_id（task 是 five_min_s2_scan）——否则 OPS-SWITCH-JOB-CONSIST 会把它
-- JOIN 成双开关打架（CL-20261124-06 踩过坑，故命名对齐 five_min_exit_sim_enabled）。
--
-- 本迁移不在发布时自动执行，须显式：
--   docker exec -i qhyc-timescaledb psql -U futures -d futures -f /app/migrations/090_five_min_s2_scan.sql

BEGIN;

INSERT INTO cfg_feature_switch
    (switch_key, switch_name, switch_group, enabled, value, description)
VALUES
    (
        'five_min_s2_sim_enabled',
        '5m S2 夜盘动能模拟盘扫描（sim broker，零真实资金）',
        'strategy',
        false,
        '',
        'CL-20261128-02。false=S2 扫描作业整轮跳过（默认，白盘链路逐位不变）；'
        || 'true=每 :05/:35 调 runner.scan_once(cfg=S2_PRESET, symbols=S2_SYMBOLS) '
        || '扫 EB888 夜盘动能信号，经 reverse 反解后由 /execution/sim/orders 落 SIM 单。'
        || '与 five_min_sim_scan_enabled（白盘扫描）完全独立，pool_id=5m-s2-sim；'
        || 'broker 必须 sim，否则作业 fail-loud。S2 离场由全局离场作业按 pool 隔离处理。'
    )
ON CONFLICT (switch_key) DO NOTHING;

INSERT INTO ops.scheduled_task
    (task_id, name, purpose, executor, schedule, schedule_desc, target, location,
     enabled, owner, tags, meta)
VALUES
    ('five_min_s2_scan',
     '5m S2 模拟盘扫描（sim，EB 夜盘动能）',
     '复用 runner.scan_once 每 :05/:35 扫 EB888，传 cfg=S2_PRESET/symbols=S2_SYMBOLS，'
     || '意图经反解后由 /execution/sim/orders 落 SIM 单；独立开关 five_min_s2_sim_enabled 控制，默认关闭',
     'apscheduler',
     '5,35 * * * *',
     '每整点后 :05/:35（与白盘扫描同节奏，夜间由信号层 session=night 门控）',
     'app.scheduler:_five_min_s2_scan_job',
     'cloud',
     true,
     'qhyc',
     ARRAY['5m', 'strategy', 'sim', 's2', 'calibration'],
     ('{"switch_key": "five_min_s2_sim_enabled", '
     || '"broker_requirement": "sim", '
     || '"cfg": "S2_PRESET", '
     || '"symbols": ["EB888"], '
     || '"pool_id": "5m-s2-sim", '
     || '"execution_enabled_independent": true, '
     || '"source_of_truth": "app/scheduler.py::_five_min_s2_scan_job"}')::jsonb
    )
ON CONFLICT (task_id) DO NOTHING;

COMMIT;
