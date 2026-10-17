-- =============================================================
-- 043_scheduled_task_registry.sql
-- 定时任务/存储过程统一登记 + 运行流水（2026-10-14 用户需求）
-- -------------------------------------------------------------
-- 目标（用户 2026-10-14 拍板）：
--   1) 把所有定时任务——数据库内跑的（TimescaleDB 后台作业）、crontab 系统任务、
--      APScheduler 注册的作业、systemd 常驻服务——以及存储过程，全部登记到
--      **数据库字典表** ops.scheduled_task（任务id/作用/是否启用/执行时间/周期）。
--   2) 后期定时任务统一通过字典表控制（enabled 总闸），不再散落在代码/配置/ crontab 里。
--   3) 每次运行的成功/失败记录落 ops.scheduled_task_run，供后期查询对账。
--
-- 背景（本项目血泪纪律）：
--   调度事实源此前分散在 5 处（app/scheduler.py 注册、scheduler_jobs.py 心跳清单、
--   宿主 crontab、TimescaleDB jobs、存储过程），任何一处变更无机制同步它处 →
--   「开关看似生效、实则静默未跑」反复发生。本迁移把「有哪些任务」收敛为
--   单一事实源（字典表），运行证据收敛到流水表。
--
-- 设计要点：
--   D1 新开 ops schema：与 app/ops、scripts/ops、/opt/qhyc-ops 命名对齐；
--      且避开 public 透传视图陷阱（public.* 存在同名 VIEW 会遮蔽 BASE TABLE）。
--   D2 种子用 INSERT ... ON CONFLICT DO UPDATE，但**只更新代码侧拥有的描述性字段**
--      （name/purpose/schedule/target/expected_gap_days/updated_at），
--      **绝不覆盖人控字段**（enabled/owner/tags/meta）——字典表是控制面，
--      改开关是运维动作，重新执行本迁移不应把它改回去。
--   D3 流水表 task_id 外键指向字典；record 侧（app/ops/task_registry.py、
--      scripts/ops/task_runner.py）对**未登记 task_id 先自动补一行最小字典行**
--      （purpose='待补录'）再写流水——宁可留下显眼的"待补录"痕迹，
--      也不让运行记录因外键静默丢失（fail-loud）。
--   D4 status CHECK 约束状态机，禁用任何隐式默认值把 unknown 变成有值。
--
-- 幂等：可重复执行（IF NOT EXISTS + ON CONFLICT）
-- 回滚：见文末 ROLLBACK 段
-- =============================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS ops;

-- ------------------------------------------------------------
-- 1. 任务字典表（控制面：单一事实源）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops.scheduled_task (
    task_id            TEXT        PRIMARY KEY,          -- 稳定标识：apscheduler 作业 id / cron_xxx / tsdb_job_N / 存储过程名 / systemd 单元名
    name               TEXT        NOT NULL,             -- 人类可读名称
    purpose            TEXT        NOT NULL,             -- 作用说明（干什么、失败会怎样）
    executor           TEXT        NOT NULL
                        CHECK (executor IN ('apscheduler','crontab','systemd','timescaledb_job','stored_proc','manual')),
    schedule           TEXT        NOT NULL,             -- 执行时间/周期：cron 表达式 / interval / '常驻' / '按需'
    schedule_desc      TEXT,                              -- 周期的人类可读说明（中文）
    target             TEXT,                              -- 执行体：命令 / 模块::函数 / 存储过程签名 / job 归属表
    location           TEXT        NOT NULL DEFAULT 'cloud',  -- 运行位置：cloud / cloud-host / cloud-container / db / local
    enabled            BOOLEAN     NOT NULL DEFAULT TRUE,-- 总开关：false = 注册门拦截（apscheduler）/ task_runner 记 disabled_skipped 跳过
    expected_gap_days  INTEGER,                           -- 心跳空档阈值（天）：距上次成功超过即疑似停摆
    owner              TEXT,                              -- 责任人（人控字段，种子不覆盖）
    tags               TEXT[]     DEFAULT '{}',           -- 标签（人控字段）
    meta               JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- 额外元数据（人控字段）
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE  ops.scheduled_task                IS '定时任务/存储过程字典（控制面单一事实源，2026-10-14）';
COMMENT ON COLUMN ops.scheduled_task.enabled        IS '总开关：false 时 apscheduler 不注册该作业、task_runner 记 disabled_skipped 跳过执行';
COMMENT ON COLUMN ops.scheduled_task.expected_gap_days IS '心跳空档阈值（天）；NULL=不纳入心跳巡检';

-- ------------------------------------------------------------
-- 2. 运行流水表（审计面：每次执行的成败证据）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops.scheduled_task_run (
    id           BIGSERIAL    PRIMARY KEY,
    task_id      TEXT         NOT NULL REFERENCES ops.scheduled_task(task_id),
    status       TEXT         NOT NULL
                 CHECK (status IN ('running','success','failed','missed','disabled_skipped','timeout')),
    trigger      TEXT         NOT NULL DEFAULT 'auto',   -- auto=调度触发 / manual=手工 / backfill=补跑
    started_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    duration_s   DOUBLE PRECISION,
    exit_code    INTEGER,                                 -- 进程类任务退出码；APScheduler 作业失败为 NULL（看 message）
    message      TEXT,                                    -- 成功摘要 / 失败异常尾部（截断 500 字）
    payload      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    host         TEXT                                     -- 执行主机/容器（可选）
);
CREATE INDEX IF NOT EXISTS idx_ops_task_run_task_time  ON ops.scheduled_task_run (task_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_ops_task_run_status     ON ops.scheduled_task_run (status, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_ops_task_run_recent     ON ops.scheduled_task_run (started_at DESC);

COMMENT ON TABLE ops.scheduled_task_run IS '定时任务运行流水（成功/失败证据，2026-10-14）';

-- ------------------------------------------------------------
-- 3. 状态视图：每任务最近一次运行 + 近 24h/7d 成败计数（查询入口）
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW ops.v_scheduled_task_status AS
SELECT t.task_id, t.name, t.executor, t.schedule, t.location, t.enabled,
       t.expected_gap_days,
       l.id            AS last_run_id,
       l.status        AS last_status,
       l.started_at    AS last_started_at,
       l.finished_at   AS last_finished_at,
       l.duration_s    AS last_duration_s,
       l.message       AS last_message,
       c24.ok_24h, c24.failed_24h,
       c7.ok_7d,  c7.failed_7d,
       -- 心跳判定：启用且有空档阈值，但近 30 天无成功记录 → 'never_ok'；
       -- 距上次成功超阈值 → 'stale'；否则 'ok'；未启用 → 'disabled'
       CASE
         WHEN NOT t.enabled THEN 'disabled'
         WHEN t.expected_gap_days IS NULL THEN 'no_heartbeat'
         WHEN l.id IS NULL OR l.started_at < now() - interval '30 days' THEN 'never_ok'
         WHEN COALESCE(l_ok.started_at, l.started_at) < now() - make_interval(days => t.expected_gap_days) THEN 'stale'
         ELSE 'ok'
       END AS heartbeat
FROM ops.scheduled_task t
LEFT JOIN LATERAL (
    SELECT * FROM ops.scheduled_task_run r
    WHERE r.task_id = t.task_id ORDER BY r.started_at DESC LIMIT 1
) l ON TRUE
LEFT JOIN LATERAL (
    SELECT * FROM ops.scheduled_task_run r
    WHERE r.task_id = t.task_id AND r.status = 'success' ORDER BY r.started_at DESC LIMIT 1
) l_ok ON TRUE
LEFT JOIN LATERAL (
    SELECT COUNT(*) FILTER (WHERE r.status = 'success') AS ok_24h,
           COUNT(*) FILTER (WHERE r.status = 'failed')  AS failed_24h
    FROM ops.scheduled_task_run r
    WHERE r.task_id = t.task_id AND r.started_at > now() - interval '24 hours'
) c24 ON TRUE
LEFT JOIN LATERAL (
    SELECT COUNT(*) FILTER (WHERE r.status = 'success') AS ok_7d,
           COUNT(*) FILTER (WHERE r.status = 'failed')  AS failed_7d
    FROM ops.scheduled_task_run r
    WHERE r.task_id = t.task_id AND r.started_at > now() - interval '7 days'
) c7 ON TRUE;

-- ------------------------------------------------------------
-- 4. 种子：登记当前全部任务（2026-10-14 快照，与代码/crontab/库内作业一致）
--    只更新描述性字段；enabled/owner/tags/meta 人控，不被覆盖。
-- ------------------------------------------------------------

-- ---- 4.1 APScheduler：app/scheduler.py::_build_scheduler（云端生产实例）----
INSERT INTO ops.scheduled_task
    (task_id, name, purpose, executor, schedule, schedule_desc, target, location, enabled, expected_gap_days)
VALUES
 ('ingest_pre_open',  '盘前采集+预测联动', '08:00 全品种日频采集，成功后联动轻模型预测；断供则当日信号无数据基础', 'apscheduler', '0 8 * * *',  '每日 08:00', 'app/scheduler.py::_ingest_job(label=pre_open)', 'cloud-container', TRUE, 3),
 ('ingest_midday',    '午盘采集+预测联动', '12:30 午盘收盘采集+预测联动', 'apscheduler', '30 12 * * *', '每日 12:30', 'app/scheduler.py::_ingest_job(label=midday)', 'cloud-container', TRUE, 3),
 ('ingest_pre_night', '晚间采集+预测联动', '20:00 晚盘采集+预测联动', 'apscheduler', '0 20 * * *', '每日 20:00', 'app/scheduler.py::_ingest_job(label=pre_night)', 'cloud-container', TRUE, 3),
 ('lstm_weekly_retrain', 'LSTM 每周重训', '周六 06:00 重训 LSTM 预测模型；停跑则预测用旧权重', 'apscheduler', '0 6 * * 6', '周六 06:00', 'app/scheduler.py::_lstm_weekly_job', 'cloud-container', TRUE, 9),
 ('transmission_weekly_recalc', '传导权重每周重算', '周六 06:30 品种间传导权重重算（LSTM 后）', 'apscheduler', '30 6 * * 6', '周六 06:30', 'app/scheduler.py::_transmission_weekly_job', 'cloud-container', TRUE, 9),
 ('backtest_weekly', '融合策略周回测', '周六 07:00 回测（权重/传导更新后）', 'apscheduler', '0 7 * * 6', '周六 07:00', 'app/scheduler.py::_backtest_weekly_job', 'cloud-container', TRUE, 9),
 ('weights_monthly_update', '模型权重月更', '每月 1 日 06:30 据近 60 日回测准确率更新模型权重', 'apscheduler', '30 6 1 * *', '每月 1 日 06:30', 'app/scheduler.py::_weights_monthly_job', 'cloud-container', TRUE, 40),
 ('fusion_scan', '融合实时信号扫描', '每 :05/:35 扫描融合信号并推送（受 fusion.enabled 配置门）', 'apscheduler', '5,35 * * * *', '每小时 :05/:35', 'app/scheduler.py::_fusion_scan_job', 'cloud-container', TRUE, 3),
 ('rank_position', '会员持仓排名采集', '每日 17:30 龙虎榜四所入库', 'apscheduler', '30 17 * * *', '每日 17:30', 'app/scheduler.py::_rank_job', 'cloud-container', TRUE, 3),
 ('warehouse_receipt', '仓单日报采集', '每日 18:40 四所仓单（scrapling 浏览器版，受 warehouse_receipt.enabled 门）', 'apscheduler', '40 18 * * *', '每日 18:40', 'app/scheduler.py::_warehouse_receipt_job', 'cloud-container', TRUE, 3),
 ('hourly_collect', '小时线增量采集', '每小时整点全品种小时线增量（受 hourly.enabled 门）', 'apscheduler', '0 * * * *', '每小时整点', 'app/scheduler.py::_hourly_job', 'cloud-container', TRUE, 3),
 ('minute_and_bars', '分钟优先管线', '每 :07/:37 1 分钟并入 minute_bar 并增量合成 bar_5/15/30/60m（内含 CALL sp_build_l1_from_minute）', 'apscheduler', '7,37 * * * *', '每小时 :07/:37', 'app/scheduler.py::_minute_and_bars_job', 'cloud-container', TRUE, 3),
 ('back_adjusted_rebuild', '后复权物化重建', '每日 02:55/11:55/15:55 重建 l2_adj.bar_*_ba（内含 CALL sp_build_l2_back_adjusted，roll_segment 签名变化自动全量；2026-10-15 由每小时 :25/:55 降频为一天三次）', 'apscheduler', '55 2,11,15 * * *', '每日 02:55/11:55/15:55', 'app/scheduler.py::_back_adjusted_job', 'cloud-container', TRUE, 3),
 ('inventory_weekly', '库存周报采集', '工作日 17:00 库存采集（受 inventory.enabled 门）', 'apscheduler', '0 17 * * 1-5', '工作日 17:00', 'app/scheduler.py::_inventory_job', 'cloud-container', TRUE, 4),
 ('spot_basis_daily', '基差/现货日频', '每日 17:10 现货价格+基差入库（受 spot_basis.enabled 门）', 'apscheduler', '10 17 * * *', '每日 17:10', 'app/scheduler.py::_spot_basis_job', 'cloud-container', TRUE, 3),
 ('daily_futures_refresh', '期货日频刷新', '每日 18:10 刷新 contract_daily 与 main_contract_map', 'apscheduler', '10 18 * * *', '每日 18:10', 'scripts/daily_futures_refresh.py::main', 'cloud-container', TRUE, 3),
 ('factor_v1v6_daily', 'V1/V6 因子日计算', '每日 16:20 因子增量计算（T22，受 factor_v1v6_enabled 开关）', 'apscheduler', '20 16 * * *', '每日 16:20', 'app/scheduler.py::_factor_v1v6_job', 'cloud-container', TRUE, 3),
 ('factor_ic_monitor_daily', '因子 IC 监控', '每日 16:45 因子 IC 监控（受 factor_ic_monitor_enabled 开关）', 'apscheduler', '45 16 * * *', '每日 16:45', 'app/scheduler.py::_factor_ic_monitor_job', 'cloud-container', TRUE, 3),
 ('data_selfcheck_daily', '数据自检', '每日 17:35 数据自检写 anomaly_ticket 工单（默认关，DATA_SELFCHECK_ENABLED=1 开启）', 'apscheduler', '35 17 * * *', '每日 17:35', 'app/scheduler.py::_data_selfcheck_job', 'cloud-container', FALSE, 3),
 ('ops_backup_daily', '运维逻辑备份', '每日备份关键小表（rebuild 前可回滚点；BACKUP_ENABLED=1 开启，时间由 BACKUP_RUN_HOUR/MINUTE 决定）', 'apscheduler', '0 4 * * *', '每日（env 可调）', 'app/scheduler.py::_backup_daily_job', 'cloud-container', TRUE, 3),
 ('ops_alert_check', '巡检告警', '每 10 分钟资源/数据新鲜度/DB uptime/作业心跳巡检（ALERT_ENABLED=1 开启）', 'apscheduler', '*/10 * * * *', '每 10 分钟', 'app/scheduler.py::_alert_check_job', 'cloud-container', TRUE, 3),
 ('scheduler_resync', '调度字典周期再同步', '每 5 分钟把 ops.scheduled_task.schedule/enabled 变更同步到内存作业，免 docker restart 即时生效（app/ops/task_registry.py::resync_jobs_from_db）', 'apscheduler', '*/5 * * * *', '每 5 分钟', 'app/ops/task_registry.py::resync_jobs_from_db', 'cloud-container', TRUE, 3)
ON CONFLICT (task_id) DO UPDATE SET
    name = EXCLUDED.name, purpose = EXCLUDED.purpose, executor = EXCLUDED.executor,
    schedule = EXCLUDED.schedule, schedule_desc = EXCLUDED.schedule_desc,
    target = EXCLUDED.target, expected_gap_days = EXCLUDED.expected_gap_days,
    updated_at = now();

-- ---- 4.2 APScheduler：本地同步专用实例（CLOUD_SYNC_ENABLED=1）----
INSERT INTO ops.scheduled_task
    (task_id, name, purpose, executor, schedule, schedule_desc, target, location, enabled, expected_gap_days)
VALUES
 ('cloud_local_sync', '云地同步', '本地开发机实例每日 03:15 从云端增量拉数（cloud2local，incremental）；仅本地 sync-only 实例注册', 'apscheduler', '15 3 * * *', '每日 03:15', 'app/scheduler.py::_sync_cloud_local_job', 'local', TRUE, 3)
ON CONFLICT (task_id) DO UPDATE SET
    name = EXCLUDED.name, purpose = EXCLUDED.purpose, executor = EXCLUDED.executor,
    schedule = EXCLUDED.schedule, schedule_desc = EXCLUDED.schedule_desc,
    target = EXCLUDED.target, expected_gap_days = EXCLUDED.expected_gap_days,
    updated_at = now();

-- ---- 4.3 APScheduler：app/pipeline/worker.py（systemd qhyc-pipeline.service）----
INSERT INTO ops.scheduled_task
    (task_id, name, purpose, executor, schedule, schedule_desc, target, location, enabled, expected_gap_days)
VALUES
 ('pipeline_daily', '信号管线日链', '每日 17:40 生成决策简报/候选清单（rank_position 后 10 分钟）', 'apscheduler', '40 17 * * *', '每日 17:40', 'app/pipeline/worker.py::_daily_job', 'cloud-host', TRUE, 3),
 ('pipeline_signal_0900',  '盘中信号推送 09:00',  'slot 09:00 信号推送',  'apscheduler', '0 9 * * *',    '每日 09:00',  'app/pipeline/worker.py::_signal_job', 'cloud-host', TRUE, 3),
 ('pipeline_signal_1000',  '盘中信号推送 10:00',  'slot 10:00 信号推送',  'apscheduler', '0 10 * * *',   '每日 10:00',  'app/pipeline/worker.py::_signal_job', 'cloud-host', TRUE, 3),
 ('pipeline_signal_1100',  '盘中信号推送 11:00',  'slot 11:00 信号推送',  'apscheduler', '0 11 * * *',   '每日 11:00',  'app/pipeline/worker.py::_signal_job', 'cloud-host', TRUE, 3),
 ('pipeline_signal_1330',  '盘中信号推送 13:30',  'slot 13:30 信号推送',  'apscheduler', '30 13 * * *',  '每日 13:30',  'app/pipeline/worker.py::_signal_job', 'cloud-host', TRUE, 3),
 ('pipeline_signal_1400',  '盘中信号推送 14:00',  'slot 14:00 信号推送',  'apscheduler', '0 14 * * *',   '每日 14:00',  'app/pipeline/worker.py::_signal_job', 'cloud-host', TRUE, 3),
 ('pipeline_signal_1500',  '盘中信号推送 15:00',  'slot 15:00 收盘信号推送', 'apscheduler', '0 15 * * *', '每日 15:00',  'app/pipeline/worker.py::_signal_job', 'cloud-host', TRUE, 3),
 ('pipeline_signal_2100',  '夜盘信号推送 21:00',  'slot 21:00 信号推送',  'apscheduler', '0 21 * * *',   '每日 21:00',  'app/pipeline/worker.py::_signal_job', 'cloud-host', TRUE, 3),
 ('pipeline_signal_2200',  '夜盘信号推送 22:00',  'slot 22:00 信号推送',  'apscheduler', '0 22 * * *',   '每日 22:00',  'app/pipeline/worker.py::_signal_job', 'cloud-host', TRUE, 3),
 ('pipeline_signal_2300',  '夜盘信号推送 23:00',  'slot 23:00 信号推送',  'apscheduler', '0 23 * * *',   '每日 23:00',  'app/pipeline/worker.py::_signal_job', 'cloud-host', TRUE, 3),
 ('pipeline_check', '管线周自检', '周一 08:05 管线数据链自检', 'apscheduler', '5 8 * * 1', '周一 08:05', 'app/pipeline/worker.py::_check_job', 'cloud-host', TRUE, 9)
ON CONFLICT (task_id) DO UPDATE SET
    name = EXCLUDED.name, purpose = EXCLUDED.purpose, executor = EXCLUDED.executor,
    schedule = EXCLUDED.schedule, schedule_desc = EXCLUDED.schedule_desc,
    target = EXCLUDED.target, expected_gap_days = EXCLUDED.expected_gap_days,
    updated_at = now();

-- ---- 4.4 systemd 常驻服务（2026-10-08 P3 全云化，迁出 Docker）----
INSERT INTO ops.scheduled_task
    (task_id, name, purpose, executor, schedule, schedule_desc, target, location, enabled, expected_gap_days)
VALUES
 ('qhyc-scheduler.service', '生产调度器常驻进程', 'APScheduler 主调度器（上述全部 apscheduler 作业的宿主进程）；挂了=全部周期作业停摆', 'systemd', '常驻', '随主机启动，restart=always', 'systemctl status qhyc-scheduler.service', 'cloud-host', TRUE, NULL),
 ('qhyc-pipeline.service',   '信号管线 worker 常驻进程', 'pipeline APScheduler（日链/信号推送/自检）；挂了=信号断更', 'systemd', '常驻', '随主机启动，restart=always', 'systemctl status qhyc-pipeline.service', 'cloud-host', TRUE, NULL)
ON CONFLICT (task_id) DO UPDATE SET
    name = EXCLUDED.name, purpose = EXCLUDED.purpose, executor = EXCLUDED.executor,
    schedule = EXCLUDED.schedule, schedule_desc = EXCLUDED.schedule_desc,
    target = EXCLUDED.target, updated_at = now();

-- ---- 4.5 宿主 crontab 系统任务（2026-10-14 快照，crontab -l 逐条一致）----
-- 改造后统一经 scripts/ops/task_runner.py 包装（查字典→执行→记流水）。
INSERT INTO ops.scheduled_task
    (task_id, name, purpose, executor, schedule, schedule_desc, target, location, enabled, expected_gap_days)
VALUES
 ('cron_ops_monitor',       '宿主资源/服务监控', '每 10 分钟巡检负载/内存/磁盘/容器/systemd/API/DB，异常推微信；与容器内 alerting 互补（容器 OOM 时唯一定位手段）', 'crontab', '*/10 * * * *', '每 10 分钟', '/usr/bin/python3 /opt/qhyc-ops/ops_monitor.py', 'cloud-host', TRUE, 1),
 ('cron_ops_monitor_daily', '监控体检日报', '每日 08:00 一份体检日报推送', 'crontab', '0 8 * * *', '每日 08:00', '/usr/bin/python3 /opt/qhyc-ops/ops_monitor.py --daily', 'cloud-host', TRUE, 3),
 ('cron_ops_cleanup',       '日志/产物清理', '每日 03:20 清理 journald/容器日志/应用日志/7 天前简报（保留账户费率状态）', 'crontab', '20 3 * * *', '每日 03:20', '/bin/bash /opt/qhyc-ops/ops_cleanup.sh', 'cloud-host', TRUE, 3),
 ('cron_fetch_1min',        '1 分钟行情补采', '每 15 分钟补采全品种 1 分钟（8000 根，flock 防重入）；分钟管线 L0 源', 'crontab', '*/15 * * * *', '每 15 分钟', 'docker exec qhyc-api python scripts/fetch_contract_1min.py --all --data-length 8000', 'cloud-host', TRUE, 1),
 ('cron_compress_chunks',   '存量 chunk 压缩', '每日 03:30 低峰窗口触发存量 chunk 压缩（压缩策略之外的人工兜底）', 'crontab', '30 3 * * *', '每日 03:30', '/Docker/qhyc/scripts/db/run_compress.sh', 'cloud-host', TRUE, 3),
 ('cron_deploy_gate',       '上线准入闸巡检', '每日 06:30 部署门禁巡检（BLOCK>0 写日志待人工处置）', 'crontab', '30 6 * * *', '每日 06:30', 'docker exec -w /app -e PYTHONPATH=/app qhyc-api python scripts/deploy_gate.py', 'cloud-host', TRUE, 3),
 ('cron_weekly_pgdump',     '周全量 pg_dump 备份', '周日 04:30 全库物理备份（保留 7 份）；ops_backup_daily 的逻辑备份之外的整库回滚点', 'crontab', '30 4 * * 0', '周日 04:30', '/home/ubuntu/qhyc_work/scripts/weekly_pgdump.sh', 'cloud-host', TRUE, 9)
ON CONFLICT (task_id) DO UPDATE SET
    name = EXCLUDED.name, purpose = EXCLUDED.purpose, executor = EXCLUDED.executor,
    schedule = EXCLUDED.schedule, schedule_desc = EXCLUDED.schedule_desc,
    target = EXCLUDED.target, expected_gap_days = EXCLUDED.expected_gap_days,
    updated_at = now();

-- ---- 4.6 数据库内定时任务：TimescaleDB 后台作业（2026-10-14 实测快照）----
INSERT INTO ops.scheduled_task
    (task_id, name, purpose, executor, schedule, schedule_desc, target, location, enabled, expected_gap_days)
VALUES
 ('tsdb_job_1',    'TimescaleDB 遥测上报', '内置：每 24h 向 TS 社区上报匿名使用统计', 'timescaledb_job', '24:00:00', '每 24 小时', 'Telemetry Reporter [1]', 'db', TRUE, 3),
 ('tsdb_job_2',    '错误日志保留策略', '内置：错误日志 1 个月保留（库内 scheduled=f，当前停用）', 'timescaledb_job', '1 mon', '每 1 个月', 'Error Log Retention Policy [2]', 'db', FALSE, NULL),
 ('tsdb_job_3',    '作业历史保留策略', '内置：job 历史 1 个月保留', 'timescaledb_job', '1 mon', '每 1 个月', 'Job History Log Retention Policy [3]', 'db', TRUE, 40),
 ('tsdb_job_1000', 'daily_bar 压缩策略', 'daily_bar chunk 每 12h 压缩', 'timescaledb_job', '12:00:00', '每 12 小时', 'hypertable=daily_bar', 'db', TRUE, 3),
 ('tsdb_job_1001', 'main_continuous 压缩策略', 'main_continuous chunk 每 12h 压缩', 'timescaledb_job', '12:00:00', '每 12 小时', 'hypertable=main_continuous', 'db', TRUE, 3),
 ('tsdb_job_1002', 'hourly_bar 压缩策略', 'hourly_bar chunk 每 12h 压缩', 'timescaledb_job', '12:00:00', '每 12 小时', 'hypertable=hourly_bar', 'db', TRUE, 3),
 ('tsdb_job_1003', '连续聚合刷新策略', 'hourly_1d_rollup 连续聚合每日刷新', 'timescaledb_job', '1 day', '每日', 'cagg=hourly_1d_rollup', 'db', TRUE, 3),
 ('tsdb_job_1004', 'bar_5m 压缩策略', 'bar_5m chunk 每 12h 压缩', 'timescaledb_job', '12:00:00', '每 12 小时', 'hypertable=bar_5m', 'db', TRUE, 3),
 ('tsdb_job_1005', 'minute_bar 压缩策略', 'minute_bar chunk 每 12h 压缩', 'timescaledb_job', '12:00:00', '每 12 小时', 'hypertable=minute_bar', 'db', TRUE, 3),
 ('tsdb_job_1006', 'fut_kline 压缩策略', 'fut_kline chunk 每 12h 压缩（表已退役为孤儿表，策略仍在，见迁移 034）', 'timescaledb_job', '12:00:00', '每 12 小时', 'hypertable=fut_kline', 'db', TRUE, 3),
 ('tsdb_job_1007', 'bar_5m_ba 压缩策略', 'l2_adj.bar_5m_ba chunk 每 12h 压缩', 'timescaledb_job', '12:00:00', '每 12 小时', 'hypertable=bar_5m_ba', 'db', TRUE, 3)
ON CONFLICT (task_id) DO UPDATE SET
    name = EXCLUDED.name, purpose = EXCLUDED.purpose, executor = EXCLUDED.executor,
    schedule = EXCLUDED.schedule, schedule_desc = EXCLUDED.schedule_desc,
    target = EXCLUDED.target, updated_at = now();

-- ---- 4.7 存储过程（用户要求一并登记；由调用方作业/手工触发）----
INSERT INTO ops.scheduled_task
    (task_id, name, purpose, executor, schedule, schedule_desc, target, location, enabled, expected_gap_days)
VALUES
 ('sp_normalize_l1_symbols',     'L1 符号规范化',           '把 L1 层符号规范为标准形态（迁移 009 建，回填/维护时调用）', 'stored_proc', '按需', '由回填脚本/手工触发', 'public.sp_normalize_l1_symbols()', 'db', TRUE, NULL),
 ('dim_symbol_parse',            '合约符号解析函数',         '把 RB2601 之类符号解析为 (exchange, product, contract)；被其他过程内部调用', 'stored_proc', '按需', '被其他过程内部调用', 'public.dim_symbol_parse(text)', 'db', TRUE, NULL),
 ('dim_symbol_collect',          '符号字典采集函数',         '从 dim_variety 采集符号集合（字典维护辅助）', 'stored_proc', '按需', '字典维护/迁移时调用', 'public.dim_symbol_collect(text,text,text)', 'db', TRUE, NULL),
 ('l1_table_of',                 '频率→L1 表名映射',         'min5→bar_5m 等频率到表名映射（纯函数，被聚合过程调用）', 'stored_proc', '按需', '被其他过程内部调用', 'public.l1_table_of(text)', 'db', TRUE, NULL),
 ('sp_build_l1_from_minute',     'L0→L1 增量聚合',           '1 分钟 → 5/15/30/60m 增量聚合（块化 upsert，缺口自动补齐）；minute_and_bars 作业每 30 分钟调一次', 'stored_proc', '随调用方', 'minute_and_bars 作业 :07/:37 调用', 'public.sp_build_l1_from_minute(text[,from,to])', 'db', TRUE, 3),
 ('sp_build_l2_roll_segment',    '换月段构建',               '构建 l2_adj.roll_segment 换月段（后复权基准）；★历史坑：从未被调度，仅回填脚本调用，price_shift 曾全 0', 'stored_proc', '按需', '回填脚本/手工触发（无周期作业）', 'public.sp_build_l2_roll_segment(text,bool)', 'db', TRUE, NULL),
 ('sp_refresh_dim_contract_dates','合约上市日期刷新',         '刷新 dim_contract_dates 合约起始日期', 'stored_proc', '按需', '字典维护/手工触发', 'public.sp_refresh_dim_contract_dates()', 'db', TRUE, NULL),
 ('sp_build_l2_back_adjusted',   'L2 后复权物化',            'l2_adj.bar_*_ba 后复权重建（roll_segment 签名变化自动全量，否则增量）；back_adjusted_rebuild 作业每 30 分钟调一次', 'stored_proc', '随调用方', 'back_adjusted_rebuild 作业 :25/:55 调用', 'l2_adj.sp_build_l2_back_adjusted(text,text,bool)', 'db', TRUE, 3)
ON CONFLICT (task_id) DO UPDATE SET
    name = EXCLUDED.name, purpose = EXCLUDED.purpose, executor = EXCLUDED.executor,
    schedule = EXCLUDED.schedule, schedule_desc = EXCLUDED.schedule_desc,
    target = EXCLUDED.target, expected_gap_days = EXCLUDED.expected_gap_days,
    updated_at = now();

COMMIT;

-- =============================================================
-- ROLLBACK（手工执行，慎之）：
--   BEGIN;
--   DROP VIEW IF EXISTS ops.v_scheduled_task_status;
--   DROP TABLE IF EXISTS ops.scheduled_task_run;
--   DROP TABLE IF EXISTS ops.scheduled_task;
--   DROP SCHEMA IF EXISTS ops;
--   COMMIT;
-- 注意：流水表是运行证据，删除前先确认无需对账。
-- =============================================================
