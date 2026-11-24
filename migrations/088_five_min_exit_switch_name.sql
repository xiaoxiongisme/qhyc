-- 088_five_min_exit_switch_name.sql
-- CL-20261124-06 修正：5m 离场开关改名，消除 OPS-SWITCH-JOB-CONSIST 双开关冲突。
--
-- 背景：087 登记的开关 five_min_exit_scan_enabled 经守卫
-- _c_switch_job_consistency 的 replace('_enabled','') 映射后恰好指向真实 task
-- five_min_exit_scan（enabled=true），开关 false vs 任务 true 被判"双开关打架"。
-- 084 的成熟做法是命名错位：开关 five_min_sim_scan_enabled → 映射 five_min_sim_scan
-- （无对应 task），由代码开关单独控制实际执行、任务字典常驻 enabled=true 注册。
--
-- 本迁移沿用该模式，仅改名，不改语义 / 默认值 / 任务字典 / 执行逻辑：
--   five_min_exit_scan_enabled → five_min_exit_sim_enabled（映射 five_min_exit_sim，无 task）
-- 代码侧 _switch_on 与守卫断言同步改用新名（同一 commit）。

BEGIN;

DELETE FROM cfg_feature_switch WHERE switch_key = 'five_min_exit_scan_enabled';

INSERT INTO cfg_feature_switch
    (switch_key, switch_name, switch_group, enabled, value, description)
VALUES
    (
        'five_min_exit_sim_enabled',
        '5m 模拟盘离场扫描（sim，零真实资金）',
        'strategy',
        false,
        '',
        'CL-20261124-06。false=离场作业整轮跳过（默认，现状逐位不变）；'
        || 'true=每 :05/:35 轮询 five_min_exit_plan 的 OPEN 计划，按 5M-EXIT-CALIBER '
        || '决策后经 /execution/sim/orders 下 CLOSE 单。'
        || '强制 broker=sim；实盘开关 five_min_pool_position_enabled 保持 off。'
        || '命名沿用 084 错位模式（映射 five_min_exit_sim 无对应 task），'
        || '由本开关单独控制执行，task five_min_exit_scan 常驻注册。'
    )
ON CONFLICT (switch_key) DO NOTHING;

COMMIT;
