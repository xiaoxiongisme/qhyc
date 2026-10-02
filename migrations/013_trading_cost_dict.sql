-- =============================================================================
-- 013 · 交易成本字典：手续费 / 滑点（可滚动 = 合约与费率都会变）
-- 日期：2026-10-02　数据层字典化（用户要求：成本口径以实际为准，区分
--       买入/卖出/平今/平昨，固定值与百分比并存，且会随合约与交易所调整而变）
-- 适用：本地库与云端库同构执行（幂等，可重复运行）
--
-- 设计要点（为什么这样建）
-- -----------------------------------------------------------------------------
-- 1) **永不覆盖，只闭合**。费率一改就 INSERT 新行并把旧行 effective_to 置为新行
--    的 effective_from。这样「回测某历史日期的手续费」能取到**当时**的费率，
--    而不是最新费率 —— 这是"滚动"的必要条件。若直接 UPDATE，历史回测会集体偏移。
-- 2) **三动作维度**。开仓 / 平昨 / 平今在期货上费率不同（苹果 5 → 平今 20；
--    焦炭标「隔日开平1‰」即隐含平今不同）。故 action 是主键的一部分。
-- 3) **两类费率**。FIXED=固定值(元/手)，PCT=按成交额万分比(‰)。单位统一为 ‰，
--    调用方不必判断量纲。FREE=平今免。
-- 4) **合约范围限定**。交易所对「特定合约」给不同费率（碳酸锂 0.8‰，但
--    2601~2702 那批 3.2‰；螺纹钢 0.2‰，1/5/10 月及 2602-2604 为 1‰）。
--    故 scope_* 两列表达限定，NULL/空 = 全范围。
-- 5) **交易所与券商加收分离**。broker_markup_* 单列，因为交易所费会变、
--    券商加收（如 +1 分/手）是账户自己的约定，二者变更节奏不同。
--
-- 与既有字典的关系
-- -----------------------------------------------------------------------------
-- * multiplier / tick_size 仍在 dim_variety（品种级不变属性）。
-- * 本表是**合约级 + 时效级**属性，故独立成表，不污染 dim_variety。
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS dim_trading_cost (
    id                  BIGSERIAL PRIMARY KEY,
    exchange            TEXT        NOT NULL REFERENCES dim_exchange(exchange_code),
    variety_code        TEXT        NOT NULL,          -- 品种码大写：MA / RB / CU
    instrument_kind     TEXT        NOT NULL DEFAULT 'FUTURE'
                        CHECK (instrument_kind IN ('FUTURE','OPTION')),
    action              TEXT        NOT NULL
                        CHECK (action IN ('OPEN','CLOSE_YEST','CLOSE_TODAY')),
    -- 合约范围限定：scope_kind='ALL' 全范围；'MONTHS' 仅限交割月；'CONTRACTS' 仅限指定合约
    scope_kind          TEXT        NOT NULL DEFAULT 'ALL'
                        CHECK (scope_kind IN ('ALL','MONTHS','CONTRACTS')),
    scope_months        INTEGER[],                       -- {1,5,10} 交割月
    scope_contracts     TEXT[],                          -- {'RB2602','RB2603'}
    fee_type            TEXT        NOT NULL
                        CHECK (fee_type IN ('FIXED','PCT','FREE')),
    -- FIXED: 元/手；PCT: ‰（万分比，成交额 = price × multiplier）
    fee_value           NUMERIC(24,10) NOT NULL DEFAULT 0,
    -- 交易所原始口径（审计用；券商加收另列）
    exchange_fee_value  NUMERIC(24,10),
    -- 券商加收（用户当前约定：+1 分/手 = 0.01 元）
    broker_markup_type  TEXT        NOT NULL DEFAULT 'NONE'
                        CHECK (broker_markup_type IN ('NONE','FIXED','PCT')),
    broker_markup_value NUMERIC(24,10) NOT NULL DEFAULT 0,
    -- 滑点（以最小变动价位计）：slip_ticks 为「每手每边跳几跳」
    slip_ticks          NUMERIC(12,4) NOT NULL DEFAULT 0,
    effective_from      DATE        NOT NULL,
    effective_to        DATE,                            -- NULL = 当前有效
    source              TEXT        NOT NULL,
    note                TEXT,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_dim_trading_cost UNIQUE
        (variety_code, instrument_kind, action, scope_kind, effective_from)
);

-- 主查询路径：某品种某动作在给定日期的有效费率
CREATE INDEX IF NOT EXISTS ix_dim_trading_cost_lookup
    ON dim_trading_cost (variety_code, instrument_kind, action, effective_from DESC);

COMMENT ON TABLE dim_trading_cost IS
    '交易成本字典（手续费/滑点，唯一权威）。按 effective_from/to 时效滚动，费率调整只追加新行不覆盖历史。'
    'fee_type=FIXED 时 fee_value 单位为 元/手；PCT 时为 ‰（按成交额万分比）。'
    '券商加收与交易所费分列，便于费率变更与账户约定各自独立维护。';
COMMENT ON COLUMN dim_trading_cost.action IS
    'OPEN=开仓/买入；CLOSE_YEST=平昨仓/卖出昨仓；CLOSE_TODAY=平今仓/卖出今仓。'
    '期货三者费率常不同（苹果开仓 5、平今 20），不可合并。';
COMMENT ON COLUMN dim_trading_cost.scope_kind IS
    'ALL=该品种全合约通用费率；MONTHS=仅限 scope_months 列出的交割月；'
    'CONTRACTS=仅限 scope_contracts 列出的合约。交易所对特定合约常给不同费率。';
COMMENT ON COLUMN dim_trading_cost.effective_to IS
    'NULL 表示当前有效。费率变更时关闭旧行（置为新行的 effective_from）再插入新行，'
    '严禁原地 UPDATE —— 否则历史回测的手续费会随费率调整而集体偏移。';

-- ---------------------------------------------------------------------------
-- ⚠ 刻意**不**为未覆盖的品种/动作预置「免费」默认行。
--
-- 初版曾为每个品种 × 3 个动作插入 fee_type='FREE', fee_value=0 的默认行，
-- 实测该做法有害：手续费来源文件对部分品种只写「隔日开平」而**不给平今**，
-- 若预置 FREE 行，就会把「**未知**」表述成「**免**」，
-- 使回测成本被系统性低估（平今费往往显著高于隔日，如苹果 5 → 20）。
--
-- 故此处不预置任何默认行：
--   * 来源覆盖到的 品种/动作 → 由 scripts/load_cost_dict.py 按实际值写入；
--   * 未覆盖的 → **该行不存在**，由 resolver 显式报「费率未知」并阻断，
--     绝不静默按 0 成本计算。需要默认值时，由用户显式拍板并写入来源标注。
--
-- 券商加收（+1 分/手 = 0.01 元）随费率行一起写入（broker_markup_* 列），
-- 不单独预置 —— 没有交易所费就没有加收的载体。
-- ---------------------------------------------------------------------------

COMMIT;
