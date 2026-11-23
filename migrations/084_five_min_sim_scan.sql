-- 084_five_min_sim_scan.sql
-- CL-20261123-02：5m 模拟盘扫描开关 + 调度任务登记。
--
-- 背景：CL-20261122-01 校准框架已入生产（CL-20261123-01），但模拟盘未启动——
-- execution_order 仅 30 行全是 11-06 前的 SIM/CANCELED 测试单，execution_fill 与
-- trade_journal 均 0 行。现有下单端点 /execution/orders 受 EXECUTION_ENABLED 门禁，
-- 而守卫 5M-EXEC-GATE 要求启用时 broker 非 sim，二者对模拟盘形成死锁。
--
-- 【默认兼容】本迁移只新增一个独立开关（默认 false）与一条调度任务登记：
--   · five_min_pool_position_enabled（实盘开关）不动；
--   · EXECUTION_ENABLED 环境变量不动；
--   · 未显式打开 five_min_sim_scan_enabled 前，作业整轮 return，行为逐位不变。
-- sim 下单走新端点 POST /execution/sim/orders，强制 channel=SIM + broker=sim。
--
-- 本迁移不在发布时自动执行，须显式：
--   docker exec -i qhyc-timescaledb psql -U futures -d futures -f /app/migrations/084_five_min_sim_scan.sql

BEGIN;

INSERT INTO cfg_feature_switch
    (switch_key, switch_name, switch_group, enabled, value, description)
VALUES
    (
        'five_min_sim_scan_enabled',
        '5m 模拟盘扫描（sim broker，零真实资金）',
        'strategy',
        false,
        '',
        'CL-20261123-02。false=5m 扫描作业整轮跳过（默认，现状逐位不变）；'
        || 'true=每 :05/:35 调 runner.scan_once 产出意图，经 /execution/sim/orders 落 SIM 单。'
        || '与 five_min_pool_position_enabled（实盘池闸）完全独立；'
        || '后者保持 off，且 EXECUTION_ENABLED 不变。broker 必须 sim，否则作业 fail-loud。'
    )
ON CONFLICT (switch_key) DO NOTHING;

INSERT INTO ops.scheduled_task
    (task_id, name, purpose, executor, schedule, schedule_desc, target, location,
     enabled, owner, tags, meta)
VALUES
    ('five_min_scan',
     '5m 模拟盘扫描（sim）',
     '复用 runner.scan_once 每 :05/:35 扫 10 品种，意图经反解后由 /execution/sim/orders 落 SIM 单；'
     || '独立开关 five_min_sim_scan_enabled 控制，默认关闭',
     'in_process',
     '5,35 * * * *',
     '每整点后 :05/:35（与 fusion/v5x 同节奏，复用现有扫描链路）',
     'app.scheduler:_five_min_scan_job',
     'cloud',
     true,
     'qhyc',
     ARRAY['5m', 'strategy', 'sim', 'calibration'],
     ('{"switch_key": "five_min_sim_scan_enabled", '
     || '"broker_requirement": "sim", '
     || '"execution_enabled_independent": true, '
     || '"source_of_truth": "app/scheduler.py::_five_min_scan_job"}')::jsonb
    )
ON CONFLICT (task_id) DO NOTHING;

COMMIT;
