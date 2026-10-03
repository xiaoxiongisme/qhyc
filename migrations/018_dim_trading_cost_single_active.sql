-- 018_dim_trading_cost_single_active.sql
-- 目标：dim_trading_cost 任一 (variety_code, instrument_kind, action, scope) 在任一时刻
--       只有一条 effective_to IS NULL 的有效行。
--
-- 背景（2026-10-03 实测）：
--   016 曾「登记为已应用」，但文件里从未包含 partial 唯一索引；多源分环
--   （交易所通知 / 郑交所官方 / akshare / 第三方文档）先后写入时旧行未被闭合，
--   同一键出现两条有效行，例如：
--     BZ  OPEN  FIXED 1.0  exchange_notice_20260311  2026-03-11 -> NULL
--     BZ  OPEN  PCT   1.0  doc_20260627              2026-10-03 -> NULL
--   读时命中哪条取决于物理顺序 -> 成本在 1permille 与 1 元/手之间静默漂移。
--   这正是 memory: 静默失效是本项目最主要的缺陷类型。

-- 1) 清理：每个键只保留生效日最新的一条有效行，其余闭合
WITH dups AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY variety_code, instrument_kind, action, scope_kind
                   ORDER BY effective_from DESC, id DESC
           ) AS rn
    FROM dim_trading_cost
    WHERE effective_to IS NULL
)
UPDATE dim_trading_cost t
SET effective_to = (SELECT MAX(x.effective_from) FROM dim_trading_cost x
                    WHERE x.variety_code = t.variety_code
                      AND x.instrument_kind = t.instrument_kind
                      AND x.action = t.action
                      AND x.scope_kind = t.scope_kind),
    updated_at = now()
FROM dups
WHERE t.id = dups.id AND dups.rn > 1;

-- 2) partial 唯一索引：从结构上杜绝重复
CREATE UNIQUE INDEX IF NOT EXISTS dim_trading_cost_one_active
    ON dim_trading_cost (variety_code, instrument_kind, action, scope_kind)
    WHERE effective_to IS NULL;

-- 3) 审计列：记录被覆盖的来源，便于追溯多源覆盖顺序
ALTER TABLE dim_trading_cost
    ADD COLUMN IF NOT EXISTS overwritten_from text;

COMMENT ON INDEX dim_trading_cost_one_active IS
    '任一费率键只允许一条有效行（018 新增；016 声称有但实际未创建）';
