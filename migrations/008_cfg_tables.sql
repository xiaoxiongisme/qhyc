-- =============================================================================
-- 008 · 配置表：cfg_feature_switch / cfg_basic_indicator / data_layer_catalog
-- 日期：2026-09-30　数据层重构（用户要求：把功能开关做成配置表由数据库控制、减少代码量）
-- 适用：本地库与云端库同构执行（幂等）
--
-- 设计目标
--   * cfg_feature_switch：原本散落在 env 变量 / 代码常量里的功能开关
--     （DATA_SELFCHECK_ENABLED / PORTFOLIO_BRAKE_ENABLED / CLOUD_SYNC_ENABLED /
--      rebuild_fut_kline_enabled / factor_* 等）统一收口到数据库，
--     由运维改一行 UPDATE 即可启停，**不必改代码、不必改 env、不必重启**依赖配置的冷路径。
--   * cfg_basic_indicator：基本面 / 仓单等指标的字典，固化「哪个指标来自哪张表、
--     什么频率、什么单位」，消除硬编码。
--   * data_layer_catalog：把 L0/L1/L2 分层落地为**治理表**，每张表的层、来源层、
--     由谁产生、可变更策略（只入库备份 / 只增改 / 只追加）一目了然，可定期审计。
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. cfg_feature_switch：数据库控制的功能开关（替代 env / 代码常量）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cfg_feature_switch (
    switch_key   text PRIMARY KEY,                     -- 唯一键，代码按此读取
    switch_name  text NOT NULL,
    switch_group text NOT NULL DEFAULT 'misc',        -- factor/ingest/sync/risk/derivation/signal/predict
    enabled      boolean NOT NULL DEFAULT false,
    value        text,                                -- 非布尔参数（如 lookback 天数）
    description  text,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_cfg_switch_group ON cfg_feature_switch(switch_group);

COMMENT ON TABLE cfg_feature_switch IS
    '功能开关配置表。代码通过 db/feature_switch.py 读取，'
    '运维改 enabled/value 即生效，避免改代码/改 env/重启。';

-- 种子：覆盖现状梳理_07 / 现状梳理_01 中出现的所有开关。
-- ⚠ 启用值与「当前 env 实际生效值」对齐，避免迁移后行为突变：
--   FACTOR_COMPUTE_ENABLED / FACTOR_IC_MONITOR_ENABLED 的 env 默认是 "1"（开），
--   故种子 enabled=true；DATA_SELFCHECK / PORTFOLIO_BRAKE 的 env 默认 "0"（关），种子=false。
--   运维后续在库内改 enabled 即生效，无需改 env/代码。
INSERT INTO cfg_feature_switch
    (switch_key, switch_name, switch_group, enabled, value, description)
VALUES
    -- 因子层（env 默认开 → 种子开，迁移后行为不变）
    ('factor_v1v6_enabled',     'V1/V6 因子计算',        'factor',  true,  NULL, 'compute_factor_v1v6 是否跑'),
    ('factor_ic_monitor_enabled','滚动 IC 监控',          'factor',  true,  NULL, 'factor_ic_monitor 是否跑'),
    -- 采集 / 入库
    ('data_selfcheck_enabled',  '数据自检',              'ingest',   false, NULL, '数据质量自检（V4）'),
    ('hourly_collect_enabled',  '小时线采集',            'ingest',   true,  NULL, 'collect_hourly 是否跑'),
    ('spot_basis_enabled',      '基差采集',              'ingest',   true,  NULL, 'collect_spot_basis 是否跑'),
    ('warehouse_receipt_enabled','仓单采集',             'ingest',   true,  NULL, 'collect_warehouse_receipt 是否跑'),
    ('member_rank_enabled',     '会员持仓采集',          'ingest',   true,  NULL, 'collect_member_rank 是否跑'),
    -- 同步
    ('cloud_sync_enabled',      '云→地同步',            'sync',     true,  '30', 'cloud_local_sync 是否注册；value=lookback 天数'),
    -- 风控
    ('portfolio_brake_enabled', '组合回撤熔断',          'risk',     false, NULL, '组合回撤熔断（V5），默认关'),
    -- 派生 / 复权
    ('rebuild_fut_kline_enabled','fut_kline 重建',       'derivation',false, NULL, 'rebuild_fut_kline 作业（带源表守卫）'),
    ('l1_canonical_enabled',    'L1 归一化刷新',         'derivation',true,  NULL, 'sp_build_l1_canonical_bars / sp_normalize_l1_symbols'),
    ('l2_roll_enabled',         'L2 换月offset刷新',     'derivation',true,  NULL, 'sp_build_l2_roll_segment（roll_segment 只追加）'),
    ('minute_bar_adj_enabled',  '1分钟复权(废弃路径)',   'derivation',false, NULL, 'minute_bar_adj 已废弃，保留开关便于审计'),
    -- 信号 / 预测
    ('briefing_signal_enabled', '简报信号产出',          'signal',   true,  NULL, 'M8 pipeline 简报信号'),
    ('prediction_enabled',      '预测留痕',             'predict',  true,  NULL, 'prediction_result 写入')
ON CONFLICT (switch_key) DO UPDATE
    SET switch_name  = EXCLUDED.switch_name,
        switch_group = EXCLUDED.switch_group,
        description  = EXCLUDED.description,
        updated_at   = now();
-- 注意：enabled/value 不在此覆盖，保留运维在库内已设的值（幂等安全）。

-- ---------------------------------------------------------------------------
-- 2. cfg_basic_indicator：基本面 / 仓单等指标字典
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cfg_basic_indicator (
    indicator_code text PRIMARY KEY,                   -- warehouse_receipt / inventory / ...
    indicator_name text NOT NULL,
    source         text,                               -- 交易所 / akshare / 天勤 / 计算
    frequency      text,                               -- daily / weekly / 事件
    unit           text,                               -- 吨 / 手 / %
    layer          text NOT NULL DEFAULT 'L0',         -- 绝大多数基本面是 L0 原始采集
    src_table      text,                               -- 对应 L0 源表
    is_active      boolean NOT NULL DEFAULT true,
    note           text,
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_cfg_basic_indicator_layer ON cfg_basic_indicator(layer);

COMMENT ON TABLE cfg_basic_indicator IS
    '基本面 / 仓单等指标字典。固化「指标→源表→频率→单位」，消除硬编码。';

INSERT INTO cfg_basic_indicator
    (indicator_code, indicator_name, source, frequency, unit, layer, src_table, note)
VALUES
    ('warehouse_receipt', '仓单日报',     '交易所',      'daily',   '吨',   'L0', 'warehouse_receipt', '四所交割仓库仓单'),
    ('inventory',         '库存',         '交易所/推算', 'weekly',  '吨',   'L0', 'inventory',         '周五 17:00，源仅 2026-05 起'),
    ('spot_basis',        '基差/现货',    'akshare',     'daily',   '元/吨','L0', 'spot_basis',        'PRD 要求 ≥2000 报告日，实测不足'),
    ('roll_yield',        '展期收益',     '计算',        'daily',   '%',    'L0', 'roll_yield',        '近远月滚动收益率'),
    ('member_position_rank','会员持仓排名','交易所',      'daily',   '手',   'L0', 'member_position_rank', '龙虎榜，symbol=合约标准码'),
    ('macro_china',       '宏观数据',     'akshare',     '月度',    NULL,   'L0', 'macro_china',       '宏观指标')
ON CONFLICT (indicator_code) DO UPDATE
    SET indicator_name = EXCLUDED.indicator_name,
        source         = EXCLUDED.source,
        frequency      = EXCLUDED.frequency,
        unit           = EXCLUDED.unit,
        src_table      = EXCLUDED.src_table,
        updated_at     = now();

-- ---------------------------------------------------------------------------
-- 3. data_layer_catalog：L0/L1/L2 分层治理表（固化数据血缘）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS data_layer_catalog (
    table_name     text PRIMARY KEY,
    layer          text NOT NULL CHECK (layer IN ('L0','L1','L2','L3')),
    purpose        text,
    source_layer   text,                               -- 由哪层派生；L0 为 NULL
    derived_by     text,                               -- 存储过程名 / 'ingest' / 'external'
    mutable_policy text NOT NULL DEFAULT 'add_modify_only'
                    CHECK (mutable_policy IN ('write_once_backup','add_modify_only','append_only')),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE data_layer_catalog IS
    '数据分层目录（治理锚点）。L0=只入库备份；L1/L2=由存储过程产生、只增改不删。';

-- 种子：对照现状梳理_01 表清单 + 数据层复权架构裁定_20260930 的 L0/L1/L2 划分
--   L0 = 直接采集的原始棒 + 基本面原始表（只入库，定期备份，不删除）
--   L1 = 由 L0 归一化/聚合出的 888/8888 棒（bar_*）+ 基本面汇总（只增改）
--   L2 = 复权 offset（roll_segment），**只追加**
--   L3 = 执行层映射（main_contract_map），已在产
INSERT INTO data_layer_catalog
    (table_name, layer, purpose, source_layer, derived_by, mutable_policy)
VALUES
    -- L0 原始采集（只入库保存 + 定期备份）
    ('fut_kline',        'L0','天勤全频原始行情(freq×kind)','NULL','ingest','write_once_backup'),
    ('daily_bar',        'L0','日线主连(akshare)','NULL','ingest','write_once_backup'),
    ('hourly_bar',       'L0','小时线主连(akshare)','NULL','ingest','write_once_backup'),
    ('minute_bar',       'L0','1分钟原始合约K线(已缺失,预留)','NULL','ingest','write_once_backup'),
    ('warehouse_receipt','L0','仓单日报原始','NULL','ingest','write_once_backup'),
    ('inventory',        'L0','库存周频原始','NULL','ingest','write_once_backup'),
    ('spot_basis',       'L0','基差/现货原始','NULL','ingest','write_once_backup'),
    ('roll_yield',       'L0','展期收益原始','NULL','ingest','write_once_backup'),
    ('member_position_rank','L0','会员持仓排名明细','NULL','ingest','write_once_backup'),
    ('macro_china',      'L0','宏观原始','NULL','ingest','write_once_backup'),
    ('contract_daily',   'L0','逐合约日线原始','NULL','ingest','write_once_backup'),
    -- L1 由 L0 派生（只增改不删）
    ('bar_5m',           'L1','5分钟归一化棒(888/8888)','L0','sp_build_l1_canonical_bars','add_modify_only'),
    ('bar_15m',          'L1','15分钟归一化棒','L0','sp_build_l1_canonical_bars','add_modify_only'),
    ('bar_30m',          'L1','30分钟归一化棒','L0','sp_build_l1_canonical_bars','add_modify_only'),
    ('bar_60m',          'L1','60分钟归一化棒','L0','sp_build_l1_canonical_bars','add_modify_only'),
    ('member_position_rank_summary','L1','会员持仓品种级汇总','L0','collect_member_rank_summary','add_modify_only'),
    -- L2 复权层（只追加不删）
    ('roll_segment',     'L2','等差后复权换月offset段','L1','sp_build_l2_roll_segment','append_only'),
    -- L3 执行层映射（已在产）
    ('main_contract_map', 'L3','主连→真实合约映射','NULL','ingest','add_modify_only')
ON CONFLICT (table_name) DO UPDATE
    SET layer         = EXCLUDED.layer,
        purpose       = EXCLUDED.purpose,
        source_layer  = EXCLUDED.source_layer,
        derived_by    = EXCLUDED.derived_by,
        mutable_policy= EXCLUDED.mutable_policy,
        updated_at    = now();

COMMIT;
