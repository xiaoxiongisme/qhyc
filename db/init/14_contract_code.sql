-- =====================================================
-- 合约代码对照表（contract_code_map）—— 全库统一的「合约代码单一真源」
--
-- 背景（2026-09-20 数据源审计结论）：
--   五所官方合约代码的「月份位数 + 大小写」各不相同，而新浪源统一用
--   「两位年 + 月、全大写」，于是**同一合约在库内出现两套 symbol**：
--
--     | 交易所 | 官方原生写法     | 新浪写法  | 标准码（本项目） |
--     |--------|------------------|-----------|------------------|
--     | CZCE   | AP701  (3 位月)  | AP2701    | AP2701           |
--     | SHFE   | cu2611 (小写)    | RB2611    | CU2611           |
--     | DCE    | a2601  (小写)    | A2611     | A2601            |
--     | GFEX   | si2611 (小写)    | SI2611    | SI2611           |
--     | CFFEX  | IF2609           | —         | IF2609           |
--
--   后果：跨源/跨所 join 对不上，下游按 symbol 聚合的持仓因子被拆成两条
--   稀疏序列。故立此表做**双向映射**，并把全库合约级 symbol 统一为标准码
--   （= 品种码大写 + YYMM 四位）。
--
-- 派生规则（见 app/core/symbol_code.py，改规则只改那里）：
--   official_symbol = to_native(std, exchange)   交易所原生（CZCE 3 位 / 其他小写）
--   sina_symbol     = to_sina(std)               新浪口径（4 位大写，与标准码同构）
--   tqsdk_symbol    = f"{exchange}.{official_symbol}"  天勤具体合约码
--
-- 不参与本表的命名空间（刻意保留原样，见 symbol_code 模块 docstring）：
--   · <品种>888            主力连续（合成）码 —— futures_symbol / daily_bar 等用
--   · KQ.m@EXCHANGE.PRODUCT 天勤主连
--   · IDX:<sector>         板块指数
--
-- 维护：scripts/build_contract_code_map.py（幂等，可反复跑）
-- =====================================================

CREATE TABLE IF NOT EXISTS contract_code_map (
    exchange        TEXT        NOT NULL,   -- SHFE / DCE / CZCE / GFEX / CFFEX / INE
    std_symbol      TEXT        NOT NULL,   -- ★标准码：品种大写 + YYMM 四位（AP2701 / CU2611）
    product         TEXT        NOT NULL,   -- 品种码（大写，如 AP）
    month_code      TEXT        NOT NULL,   -- 月份码 YYMM（如 2701）
    deliv_year      INT         NOT NULL,   -- 交割年（2027）
    deliv_month     INT         NOT NULL,   -- 交割月（1~12）
    official_symbol TEXT,                   -- 交易所官方原生写法（AP701 / cu2611）
    sina_symbol     TEXT,                   -- 新浪写法（AP2701 / RB2611）
    tqsdk_symbol    TEXT,                   -- 天勤具体合约写法（CZCE.AP701 / SHFE.cu2611）
    observed_native TEXT,                   -- ★库内实际出现过的原生写法（逗号分隔，审计留痕）
    sources         TEXT,                   -- ★出现过的数据源 src（逗号分隔）
    name            TEXT,                   -- 品种中文名（取自 futures_symbol，可空）
    first_seen      DATE,                   -- 库内最早观测日
    last_seen       DATE,                   -- 库内最晚观测日
    version         TEXT        NOT NULL DEFAULT 'v1.0',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (exchange, std_symbol, version)
);

-- 按标准码查（跨所聚合的主力查询路径）
CREATE INDEX IF NOT EXISTS idx_ccm_std     ON contract_code_map (std_symbol);
-- 按原生码反查（把交易所/新浪吐出来的码翻译成标准码）
CREATE INDEX IF NOT EXISTS idx_ccm_official ON contract_code_map (official_symbol);
CREATE INDEX IF NOT EXISTS idx_ccm_sina     ON contract_code_map (sina_symbol);
CREATE INDEX IF NOT EXISTS idx_ccm_product  ON contract_code_map (exchange, product);
