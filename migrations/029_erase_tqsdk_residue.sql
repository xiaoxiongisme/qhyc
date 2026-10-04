-- =============================================================================
-- 029 · 彻底擦除天勤残留（C9 去天勤收尾）—— 受控迁移
-- 日期：2026-10-04
--
-- 背景：
--   C9 去天勤后代码层已清零（grep -rn 'tqsdk|TqApi' app scripts --include=*.py = 0 行），
--   但库内 schema 仍残留 4 处天勤字样，本迁移一并中性化，使 live schema 不再含该字样：
--     1) contract_code_map.tqsdk_symbol  —— 与同表 native_symbol 值完全相同
--        （两者均为 f"{exchange}.{official_symbol}"，见 db/init/14_contract_code.sql 派生规则），
--        属冗余列 → 删列。
--     2) anomaly_ticket.tqsdk_val        —— 孤儿历史比对列（无任何代码读写）→ 改名 legacy_val，保留数据。
--     3) dim_symbol.namespace = 'tqsdk'   —— KQ.m@ 原生连续码命名空间 → 改名 'kq'。
--     4) v_symbol_canonical.tqsdk_symbol —— 视图别名 → kq_symbol；子查询命名空间 → 'kq'。
--        dim_symbol_parse() 对 KQ.m@ 的返回值 → 'kq'（否则新解析会再写回旧命名空间）。
--
-- 幂等：每项均先做存在性判断，可重复执行；对已是目标形态的库（含按新 db/init + 新 001/003
--       全新构建的库）为无操作，不报错。
--
-- ⚠️ 受控迁移：本文件改 live schema（云端 timescaledb + 本地库）。应用前须按文末
--    「验收 SQL」在目标库复核；建议先本地库、后云端（经 15432 隧道）各执行一次。
-- =============================================================================

BEGIN;

-- 1) contract_code_map：删除与 native_symbol 重复的冗余列
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'contract_code_map' AND column_name = 'tqsdk_symbol'
  ) THEN
    ALTER TABLE contract_code_map DROP COLUMN tqsdk_symbol;
    RAISE NOTICE 'contract_code_map.tqsdk_symbol 已删列';
  ELSE
    RAISE NOTICE 'contract_code_map.tqsdk_symbol 不存在，跳过';
  END IF;
END $$;

-- 2) anomaly_ticket：孤儿历史比对列改名保留数据
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'anomaly_ticket' AND column_name = 'tqsdk_val'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'anomaly_ticket' AND column_name = 'legacy_val'
  ) THEN
    ALTER TABLE anomaly_ticket RENAME COLUMN tqsdk_val TO legacy_val;
    RAISE NOTICE 'anomaly_ticket.tqsdk_val 已改名为 legacy_val';
  ELSE
    RAISE NOTICE 'anomaly_ticket 列名已就绪或不存在，跳过';
  END IF;
END $$;

-- 3) dim_symbol 命名空间重命名（旧 → kq）
UPDATE dim_symbol SET namespace = 'kq' WHERE namespace = 'tqsdk';

-- 4) dim_symbol_parse：KQ.m@ 解析结果命名空间与 003 保持一致（kq）
CREATE OR REPLACE FUNCTION dim_symbol_parse(s text)
RETURNS TABLE(product text, namespace text, exchange text) AS $$
DECLARE
    m text[];
BEGIN
    -- 天勤原生连续码：KQ.m@DCE.A
    IF s ~ '^KQ\.m@[A-Z]+\.[A-Z0-9]+$' THEN
        m := regexp_match(s, '^KQ\.m@([A-Z]+)\.([A-Z0-9]+)$');
        RETURN QUERY SELECT upper(m[2]), 'kq'::text, m[1];
    -- 交易所.品种年月：SHFE.au2601 / CZCE.CF009
    ELSIF s ~ '^[A-Z]{4,5}\.[A-Za-z]+[0-9]{3,4}$' THEN
        m := regexp_match(s, '^([A-Z]{4,5})\.([A-Za-z]+)([0-9]{3,4})$');
        RETURN QUERY SELECT upper(m[2]), 'contract'::text, m[1];
    -- 指数连：A8888
    ELSIF s ~ '^[A-Za-z0-9]+8888$' THEN
        m := regexp_match(s, '^([A-Za-z0-9]+)8888$');
        RETURN QUERY SELECT upper(m[1]), 'index'::text, NULL::text;
    -- 主连：A888
    ELSIF s ~ '^[A-Za-z]+888$' THEN
        m := regexp_match(s, '^([A-Za-z]+)888$');
        RETURN QUERY SELECT upper(m[1]), 'main'::text, NULL::text;
    -- 标准合约：AP2701
    ELSIF s ~ '^[A-Za-z]+[0-9]{4}$' THEN
        m := regexp_match(s, '^([A-Za-z]+)([0-9]{4})$');
        RETURN QUERY SELECT upper(m[1]), 'contract'::text, NULL::text;
    ELSE
        RETURN QUERY SELECT upper(s), 'other'::text, NULL::text;
    END IF;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- 5) v_symbol_canonical：别名与子查询命名空间同步为 kq
--    注意：Postgres 不允许 CREATE OR REPLACE VIEW 改变列名（报
--    "cannot change name of view column"），必须先用 ALTER VIEW ... RENAME COLUMN 改别名，
--    之后 CREATE OR REPLACE 才能对齐列名。
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'v_symbol_canonical' AND column_name = 'tqsdk_symbol'
  ) THEN
    ALTER VIEW v_symbol_canonical RENAME COLUMN tqsdk_symbol TO kq_symbol;
    RAISE NOTICE 'v_symbol_canonical.tqsdk_symbol 已改名为 kq_symbol';
  END IF;
END $$;

CREATE OR REPLACE VIEW v_symbol_canonical AS
SELECT product,
       max(symbol) FILTER (WHERE namespace='main'     AND is_preferred) AS main_symbol,
       max(symbol) FILTER (WHERE namespace='index'    AND is_preferred) AS index_symbol,
       max(symbol) FILTER (WHERE namespace='contract' AND is_preferred) AS contract_symbol,
       max(symbol) FILTER (WHERE namespace='kq'       AND is_preferred) AS kq_symbol,
       count(*) AS symbol_count,
       count(*) FILTER (WHERE namespace='kq') > 0
         AND count(*) FILTER (WHERE namespace='main') > 0 AS has_namespace_conflict
FROM dim_symbol
GROUP BY product;

COMMENT ON VIEW v_symbol_canonical IS
    '品种 → 各命名空间首选符号。has_namespace_conflict=true 表示该品种同时存在 '
    '888 与 KQ.m@ 两套码，跨周期 join 前必须显式指定取哪个（BarStore 默认取 main）。';

COMMIT;

-- =============================================================================
-- 验收 SQL（应用后逐条执行，括号内为期望结果）
-- =============================================================================
-- SELECT count(*) FROM dim_symbol WHERE namespace = 'tqsdk';                    -- 0
-- SELECT count(*) FROM dim_symbol WHERE namespace = 'kq';                       -- >0（等于原 tqsdk 行数）
-- SELECT count(*) FROM information_schema.columns
--   WHERE table_name='contract_code_map' AND column_name='tqsdk_symbol';       -- 0
-- SELECT count(*) FROM information_schema.columns
--   WHERE table_name='anomaly_ticket' AND column_name='tqsdk_val';             -- 0
-- SELECT count(*) FROM information_schema.columns
--   WHERE table_name='anomaly_ticket' AND column_name='legacy_val';            -- 1
-- SELECT * FROM dim_symbol_parse('KQ.m@DCE.A');                                -- namespace = kq
-- SELECT count(*) FROM information_schema.columns
--   WHERE table_name='v_symbol_canonical' AND column_name='tqsdk_symbol';      -- 0
-- SELECT count(*) FROM information_schema.columns
--   WHERE table_name='v_symbol_canonical' AND column_name='kq_symbol';         -- 1
-- SELECT count(*) FROM v_symbol_canonical WHERE kq_symbol IS NOT NULL;          -- 0 属正常：视图按 is_preferred 暴露 kq，
--                                                                                 -- 仅当某品种"无 main"时 kq 才入选；本地实测 27 个 kq 品种均有 main，故全 NULL。
--                                                                                 -- 关键是列存在且视图可出数（view_rows>0、main_symbol 非空>0）。
-- -- 全库 schema 级残留自查（应仅命中本文件注释）：
-- SELECT table_name, column_name FROM information_schema.columns
--   WHERE column_name ILIKE '%tqsdk%';                                          -- 0 行
-- SELECT routine_name FROM information_schema.routines
--   WHERE routine_definition ILIKE '%tqsdk%';                                   -- 0 行
-- SELECT viewname FROM pg_views WHERE definition ILIKE '%tqsdk%';               -- 0 行
-- -- 代码/文本层残留自查（仓库内）：
-- grep -rn 'tqsdk' db/init migrations --include=*.sql | grep -v '029_'           -- 0 行
-- grep -rn 'tqsdk\|TqApi' app scripts --include=*.py                            -- 0 行
