-- 082_blindspot_schema.sql
-- CL-20261116-01：blindspot「少数面发现」独立只读分区。
--
-- 背景与边界
-- ----------------------------------------------------
-- 把本地 blindspot 项目（E:/抖音分析/2026-11-15-21-21-33）的自然语言资讯/
-- 公告/信号与 PIT 快照迁入 qhyc 统一查看，用户 2026-11-16 拍板 qhyc 为唯一真源。
--
-- 【硬隔离】这是 L0 阅读线索，明确「可用于阅读、不可用于交易参考」，经
-- improve-thinking 审查后确定为研究展示数据。本 schema 与交易链路物理隔离：
--   * 不写 l0_raw / l1_mkt / l2_adj / l3_ref / public 的任何交易表
--   * 任何交易 SP / 视图 / 融合信号 / 预测 / 回测均不得引用本 schema
--   * 只通过专用只读 API + 前端「少数面发现」页面消费
-- 由不变量 BS-ISOLATED-FROM-TRADING 与 BS-SCHEMA-WIRED 强制。
--
-- 数据口径：完全镜像 SQLite 源库结构（TEXT 主键保留 asof/code 字符串口径），
-- 由同步脚本 blindspot_sync.py 幂等 upsert；本 migration 只建结构不灌数据。
--
-- 回滚：DROP SCHEMA blindspot CASCADE;（不影响任何交易表）

BEGIN;

CREATE SCHEMA IF NOT EXISTS blindspot;

COMMENT ON SCHEMA blindspot IS
  'L0 阅读线索（少数面发现），只读展示；与交易链路硬隔离，禁止被交易 SP/视图引用。CL-20261116-01';

-- 采集条目（资讯/公告/通知/长尾帖）
CREATE TABLE IF NOT EXISTS blindspot.items (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL UNIQUE,
    published   TEXT,
    content     TEXT,
    varieties   TEXT,
    direction   INTEGER DEFAULT 0,
    attention   INTEGER DEFAULT 0,
    raw         TEXT,
    fetched_at  TEXT NOT NULL,
    asof        TEXT
);
CREATE INDEX IF NOT EXISTS ix_bs_items_asof ON blindspot.items (asof);
CREATE INDEX IF NOT EXISTS ix_bs_items_kind ON blindspot.items (kind);

-- 行情快照
CREATE TABLE IF NOT EXISTS blindspot.quotes (
    asof        TEXT NOT NULL,
    code        TEXT NOT NULL,
    name        TEXT,
    variety     TEXT,
    chg         REAL,
    vol         REAL,
    amount      REAL,
    market      INTEGER,
    oi          REAL,
    oi_chg      REAL,
    snapshot_at TEXT,
    last        REAL,
    PRIMARY KEY (asof, code)
);

-- 四类信号（非交易信号，仅展示）
CREATE TABLE IF NOT EXISTS blindspot.signals (
    asof     TEXT NOT NULL,
    variety  TEXT NOT NULL,
    stype    TEXT NOT NULL,
    score    REAL,
    evidence TEXT,
    detail   TEXT,
    PRIMARY KEY (asof, variety, stype)
);
CREATE INDEX IF NOT EXISTS ix_bs_signals_asof ON blindspot.signals (asof);

-- 运行记录
CREATE TABLE IF NOT EXISTS blindspot.runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT,
    finished_at TEXT,
    stats       TEXT,
    notes       TEXT
);

-- PIT 快照（append-only 语义由同步脚本保证：不覆盖历史 asof+hash）
CREATE TABLE IF NOT EXISTS blindspot.pit_snapshots (
    asof            TEXT NOT NULL,
    config_hash     TEXT NOT NULL,
    captured_at     TEXT NOT NULL,
    run_id          TEXT,
    n_signals       INTEGER,
    n_quotes        INTEGER,
    n_priced        INTEGER,
    data_cutoff     TEXT,
    signal_ready_at TEXT,
    source_health   TEXT,
    degraded        INTEGER DEFAULT 0,
    degrade_reasons TEXT,
    payload_hash    TEXT NOT NULL,
    PRIMARY KEY (asof, config_hash)
);

CREATE TABLE IF NOT EXISTS blindspot.pit_signals (
    asof        TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    variety     TEXT NOT NULL,
    stype       TEXT NOT NULL,
    score       REAL,
    evidence    TEXT,
    detail      TEXT,
    PRIMARY KEY (asof, config_hash, variety, stype)
);

CREATE TABLE IF NOT EXISTS blindspot.pit_prices (
    asof        TEXT NOT NULL,
    code        TEXT NOT NULL,
    variety     TEXT,
    last        REAL,
    snapshot_at TEXT,
    PRIMARY KEY (asof, code)
);

-- 元信息：记录同步水位与口径指纹，便于前端展示"数据新鲜度"
CREATE TABLE IF NOT EXISTS blindspot.sync_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE blindspot.sync_meta IS '同步水位/口径指纹/来源路径等元信息，由 blindspot_sync.py 维护';

-- 只读展示视图（API 统一从这里读；视图名前缀 v_ 便于不变量扫描）
CREATE OR REPLACE VIEW blindspot.v_latest_snapshot AS
SELECT asof, config_hash, captured_at, n_signals, n_quotes, n_priced,
       degraded, degrade_reasons, data_cutoff, signal_ready_at
FROM blindspot.pit_snapshots
ORDER BY asof DESC, captured_at DESC
LIMIT 1;

CREATE OR REPLACE VIEW blindspot.v_signal_board AS
SELECT asof, variety, stype, score, evidence
FROM blindspot.signals
WHERE asof = (SELECT MAX(asof) FROM blindspot.signals)
ORDER BY score DESC, variety, stype;

COMMIT;
